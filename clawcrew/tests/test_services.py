"""the self-hosted tool clients: n8n and dify, spoken to over http.

no vendor code is involved here either way: these tests assert the requests
clawcrew makes to *your* instance, using `httpx.MockTransport`, and that a workflow
node can hand a step to one of them and record what came back.
"""

from __future__ import annotations

import json
import unittest

import httpx

from base import AppCase

import base  # noqa: F401

from core.config import Settings
from core.errors import ProviderFailure, ProviderNotConfigured, ValidationError
from toolbox.workflows import dify as dify_mod
from toolbox.workflows import n8n as n8n_mod
from core.services import build_services
from work.workflows import validate_definition


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        self.requests.append(request)
        self.bodies = getattr(self, "bodies", []) + [body]
        if not self.responses:
            return httpx.Response(200, json={})
        status, payload = self.responses.pop(0)
        if isinstance(payload, (bytes, bytearray)) or payload == b"":
            return httpx.Response(status, content=payload)
        return httpx.Response(status, json=payload)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def json(self, index: int = 0) -> dict:
        return json.loads(self.bodies[index] or b"{}")


class N8NTests(unittest.TestCase):
    def client(self, recorder: Recorder) -> n8n_mod.N8NClient:
        return n8n_mod.make("http://n8n.internal:5678", "n8n_key", transport=recorder.transport)

    def test_trigger_posts_payload_to_the_webhook(self):
        recorder = Recorder((200, {"ok": True, "rows": 3}))
        result = self.client(recorder).trigger("digest", {"sources": ["hn"]})
        request = recorder.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/webhook/digest")
        self.assertEqual(request.headers["x-n8n-api-key"], "n8n_key")
        self.assertEqual(recorder.json(), {"sources": ["hn"]})
        self.assertTrue(result["responded"])
        self.assertEqual(result["result"]["rows"], 3)

    def test_test_mode_uses_the_editor_webhook(self):
        recorder = Recorder((200, {"ok": True}))
        self.client(recorder).trigger("digest", {}, test=True)
        self.assertEqual(recorder.requests[0].url.path, "/webhook-test/digest")

    def test_webhook_failure_surfaces_status_and_body(self):
        recorder = Recorder((404, {"message": "webhook not registered"}))
        with self.assertRaises(ProviderFailure) as ctx:
            self.client(recorder).trigger("ghost")
        self.assertEqual(ctx.exception.details.get("status"), 404)

    def test_api_prefix_is_not_doubled(self):
        recorder = Recorder((200, {"data": []}))
        client = n8n_mod.make("http://n8n.internal:5678/api/v1/", "k", transport=recorder.transport)
        client.list_workflows()
        self.assertEqual(recorder.requests[0].url.path, "/api/v1/workflows")

    def test_list_and_export_workflows(self):
        workflow = {"id": "w1", "name": "digest", "nodes": [{"name": "S"}], "connections": {}}
        recorder = Recorder((200, {"data": [workflow]}), (200, workflow))
        client = self.client(recorder)
        rows = client.list_workflows(active=True)
        self.assertEqual(rows[0]["id"], "w1")
        self.assertIn("active=true", str(recorder.requests[0].url))
        exported = client.export_workflow("w1")
        self.assertEqual(exported["name"], "digest")
        self.assertEqual(recorder.requests[1].url.path, "/api/v1/workflows/w1")

    def test_activate_hits_the_activation_route(self):
        recorder = Recorder((200, {"active": True}))
        self.client(recorder).activate("w1")
        self.assertEqual(recorder.requests[0].url.path, "/api/v1/workflows/w1/activate")

    def test_unconfigured_client_refuses(self):
        with self.assertRaises(ProviderNotConfigured):
            n8n_mod.make("http://n8n.internal:5678", "").list_workflows()


class DifyTests(unittest.TestCase):
    def client(self, recorder: Recorder) -> dify_mod.DifyClient:
        return dify_mod.make("http://dify.internal/v1", "app-key", transport=recorder.transport)

    def test_run_workflow_sends_inputs_and_returns_the_data_block(self):
        recorder = Recorder((200, {"workflow_run_id": "r1", "data": {"status": "succeeded",
                                                                    "outputs": {"text": "hi"}}}))
        data = self.client(recorder).run_workflow({"topic": "ai"}, user="anne")
        request = recorder.requests[0]
        self.assertEqual(request.url.path, "/v1/workflows/run")
        self.assertEqual(request.headers["authorization"], "Bearer app-key")
        body = recorder.json()
        self.assertEqual(body["inputs"], {"topic": "ai"})
        self.assertEqual(body["user"], "anne")
        self.assertEqual(body["response_mode"], "blocking")
        self.assertEqual(data["outputs"], {"text": "hi"})

    def test_base_url_without_v1_gets_it(self):
        recorder = Recorder((200, {"name": "app"}))
        dify_mod.make("http://dify.internal", "app-key", transport=recorder.transport).info()
        self.assertEqual(recorder.requests[0].url.path, "/v1/info")

    def test_failure_surfaces_as_provider_failure(self):
        recorder = Recorder((400, {"code": "invalid_param", "message": "inputs are required"}))
        with self.assertRaises(ProviderFailure) as ctx:
            self.client(recorder).run_workflow({})
        self.assertEqual(ctx.exception.details.get("status"), 400)

    def test_unconfigured_client_refuses(self):
        with self.assertRaises(ProviderNotConfigured):
            dify_mod.make("http://dify.internal", "").info()


class ServiceRegistryTests(unittest.TestCase):
    def test_status_reports_both_tools_and_needs(self):
        settings = Settings(n8n_api_key="k", dify_api_key="app-k")
        status = build_services(settings).status()
        self.assertTrue(status["n8n"]["configured"])
        self.assertTrue(status["dify"]["configured"])
        self.assertTrue(status["n8n"]["self_hosted"])
        self.assertIn("n8n API", status["n8n"]["needs"])


class WorkflowNodeTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.crew = self.app.crews.create_team("ops", mission="run the shop")
        self.n8n_recorder = Recorder((200, {"ok": True, "digest": "three headlines"}))
        self.app.services.n8n = n8n_mod.make(
            "http://n8n.internal:5678", "k", transport=self.n8n_recorder.transport
        )
        self.dify_recorder = Recorder(
            (200, {"data": {"status": "succeeded", "outputs": {"reply": "shipped"}}})
        )
        self.app.services.dify = dify_mod.make(
            "http://dify.internal/v1", "app-k", transport=self.dify_recorder.transport
        )

    def run_workflow(self, definition: dict) -> dict:
        validate_definition(definition)
        workflow = self.app.workflows.save(self.crew["id"], "nightly", definition)
        return self.app.workflows.run(workflow["id"])

    def test_n8n_node_delegates_and_records_the_result(self):
        run = self.run_workflow({
            "nodes": [
                {"id": "seed", "type": "set_context", "key": "topic", "value": "agents"},
                {"id": "digest", "type": "n8n", "path": "digest",
                 "payload": {"topic": "${seed}"}, "depends_on": ["seed"]},
            ]
        })
        self.assertEqual(run["state"]["nodes"]["digest"]["status"], "succeeded")
        self.assertEqual(run["state"]["outputs"]["digest"]["digest"], "three headlines")
        self.assertEqual(self.n8n_recorder.requests[0].url.path, "/webhook/digest")

    def test_dify_node_delegates_and_records_outputs(self):
        run = self.run_workflow({
            "nodes": [
                {"id": "reply", "type": "dify", "inputs": {"draft": "hello"}},
            ]
        })
        self.assertEqual(run["state"]["outputs"]["reply"], {"reply": "shipped"})
        body = self.dify_recorder.json()
        self.assertEqual(body["inputs"], {"draft": "hello"})

    def test_dify_failure_fails_the_node(self):
        self.app.services.dify = dify_mod.make(
            "http://dify.internal/v1", "app-k",
            transport=Recorder((200, {"data": {"status": "failed", "error": "node blew up"}})).transport,
        )
        run = self.run_workflow({"nodes": [{"id": "reply", "type": "dify", "inputs": {}}]})
        self.assertEqual(run["state"]["status"], "failed")
        self.assertIn("node blew up", run["state"]["nodes"]["reply"]["error"])

    def test_n8n_node_without_a_path_is_rejected_at_save_time(self):
        with self.assertRaises(ValidationError):
            validate_definition({"nodes": [{"id": "digest", "type": "n8n"}]})


if __name__ == "__main__":
    unittest.main()
