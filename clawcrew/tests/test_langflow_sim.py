"""langflow and sim as workflow nodes: your instance, over its api, nothing vendored."""

from __future__ import annotations

import json
import unittest

import httpx

from base import AppCase

from core.errors import ProviderFailure, ProviderNotConfigured, ValidationError
from integrations.langflow import LangflowClient, parse_run
from integrations.sim import SimClient, parse_execution
from work.workflows import validate_definition

LANGFLOW_ENVELOPE = {
    "session_id": "s-1",
    "outputs": [{"outputs": [{"results": {"message": {"text": "the flow said this"}}}]}],
}


class LangflowClientTests(unittest.TestCase):
    def test_a_run_posts_to_the_flow_api_with_the_key(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["key"] = request.headers.get("x-api-key")
            seen["body"] = json.loads(request.content or b"{}")
            return httpx.Response(200, json=LANGFLOW_ENVELOPE)

        client = LangflowClient(base_url="http://langflow:7860", api_key="lf_key",
                                transport=httpx.MockTransport(handler))
        result = client.run_flow("flow_1", "write something")
        self.assertEqual(seen["path"], "/api/v1/run/flow_1")
        self.assertEqual(seen["key"], "lf_key")
        self.assertEqual(seen["body"]["input_value"], "write something")
        self.assertEqual(seen["body"]["output_type"], "chat")
        self.assertEqual(result["text"], "the flow said this")

    def test_a_base_url_with_api_v1_is_not_doubled(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            return httpx.Response(200, json=LANGFLOW_ENVELOPE)

        client = LangflowClient(base_url="http://langflow:7860/api/v1", api_key="k",
                                transport=httpx.MockTransport(handler))
        client.run_flow("f", "x")
        self.assertEqual(seen["path"], "/api/v1/run/f")

    def test_an_unconfigured_instance_is_loud(self) -> None:
        client = LangflowClient(base_url="http://langflow:7860", api_key="")
        self.assertFalse(client.configured())
        self.assertIn("CLAWCREW_LANGFLOW_API_KEY", client.status()["needs"])
        with self.assertRaises(ProviderNotConfigured):
            client.run_flow("f", "x")

    def test_an_error_status_becomes_a_provider_failure(self) -> None:
        client = LangflowClient(base_url="http://l", api_key="k",
                                transport=httpx.MockTransport(
                                    lambda request: httpx.Response(500, text="boom")))
        with self.assertRaises(ProviderFailure):
            client.run_flow("f", "x")

    def test_parse_tolerates_a_flat_envelope(self) -> None:
        self.assertEqual(parse_run({"text": "flat"})["text"], "flat")


class SimClientTests(unittest.TestCase):
    def test_execute_posts_the_input_and_lifts_the_output(self) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["key"] = request.headers.get("x-api-key")
            seen["body"] = json.loads(request.content or b"{}")
            return httpx.Response(200, json={"success": True, "executionId": "ex_1",
                                             "output": {"text": "done"}})

        client = SimClient(base_url="http://sim:3000", api_key="sim_key",
                           transport=httpx.MockTransport(handler))
        result = client.execute("wf_1", {"topic": "launch"})
        self.assertEqual(seen["path"], "/api/workflows/wf_1/execute")
        self.assertEqual(seen["key"], "sim_key")
        self.assertEqual(seen["body"]["input"], {"topic": "launch"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["execution_id"], "ex_1")
        self.assertEqual(result["output"], {"text": "done"})

    def test_a_failed_execution_is_not_ok(self) -> None:
        parsed = parse_execution({"success": False, "error": "node blew up"})
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["error"], "node blew up")

    def test_an_unconfigured_instance_is_loud(self) -> None:
        client = SimClient(base_url="http://sim:3000", api_key="")
        self.assertIn("CLAWCREW_SIM_API_KEY", client.status()["needs"])
        with self.assertRaises(ProviderNotConfigured):
            client.execute("wf", {})

    def test_an_empty_workflow_id_is_refused(self) -> None:
        client = SimClient(base_url="http://sim:3000", api_key="k")
        with self.assertRaises(ProviderFailure):
            client.execute("", {})


class WorkflowNodeTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("studio")

    def _workflow(self, node: dict) -> dict:
        definition = {"nodes": [node]}
        validate_definition(definition)
        return self.app.workflows.save(self.team["id"], "flows", definition)

    def test_a_langflow_node_runs_the_flow_and_keeps_the_text(self) -> None:
        workflow = self._workflow({"id": "n1", "type": "langflow", "flow_id": "flow_9",
                                   "input": "summarise the card"})
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            return httpx.Response(200, json=LANGFLOW_ENVELOPE)

        self.app.workflows.services.langflow = LangflowClient(
            base_url="http://langflow:7860", api_key="k", transport=httpx.MockTransport(handler))
        run = self.app.workflows.run(workflow["id"])
        self.assertEqual(seen["path"], "/api/v1/run/flow_9")
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["state"]["outputs"]["n1"], "the flow said this")

    def test_a_sim_node_runs_the_workflow(self) -> None:
        workflow = self._workflow({"id": "n1", "type": "sim", "workflow_id": "wf_2",
                                   "inputs": {"topic": "launch"}})

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/workflows/wf_2/execute")
            return httpx.Response(200, json={"success": True, "output": {"text": "ok"}})

        self.app.workflows.services.sim = SimClient(
            base_url="http://sim:3000", api_key="k", transport=httpx.MockTransport(handler))
        run = self.app.workflows.run(workflow["id"])
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["state"]["outputs"]["n1"], {"text": "ok"})

    def test_a_failed_sim_execution_fails_the_node(self) -> None:
        workflow = self._workflow({"id": "n1", "type": "sim", "workflow_id": "wf_3"})
        self.app.workflows.services.sim = SimClient(
            base_url="http://sim:3000", api_key="k",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"success": False, "error": "node blew up"})))
        run = self.app.workflows.run(workflow["id"])
        self.assertEqual(run["status"], "failed")

    def test_a_langflow_node_needs_a_flow_id(self) -> None:
        with self.assertRaises(ValidationError):
            validate_definition({"nodes": [{"id": "n1", "type": "langflow"}]})

    def test_a_sim_node_needs_a_workflow_id(self) -> None:
        with self.assertRaises(ValidationError):
            validate_definition({"nodes": [{"id": "n1", "type": "sim"}]})


if __name__ == "__main__":
    unittest.main()
