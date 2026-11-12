"""the two hosted adapters, exercised through their vendored generated clients.

nothing is stubbed at the adapter boundary any more. an `httpx.MockTransport` is
injected below the generated client, so every assertion is about the request that
the vendor's own code builds: path, auth header, and the json body produced by the
vendor's own `to_dict`. that is the point of vendoring them instead of hand-writing
the routes.
"""

from __future__ import annotations

import json
import unittest

import httpx

import base  # noqa: F401  (puts the backend on sys.path)

from core.errors import ProviderFailure, ProviderNotConfigured
from sandbox import Handle
from ingredients.machines.persistent.client import PersistentProvider
from ingredients.machines.ephemeral.client import EphemeralProvider
from sandbox.transport import api_root

PERSISTENT_COMPUTER = {
    "id": "cmp_1",
    "name": "agent-a",
    "status": "running",
    "vcpus": 2,
    "ram_mb": 2048,
    "disk_size_mb": 8192,
    "image": "clawcrew/persistent:latest",
    "compute_location": {"provider": "persistent", "region": "us-east", "country_code": "US"},
    "created_at": "2026-11-10T00:00:00Z",
    "connection": {"ssh": "ssh://x", "access_url": "https://y"},
}

EPHEMERAL_SANDBOX = {
    "sandboxID": "sbx_1",
    "templateID": "base",
    "clientID": "cl_1",
    "envdVersion": "0.2.0",
    "envdAccessToken": "secret-token",
    "domain": "ephemeral.internal",
}

EPHEMERAL_DETAIL = {
    **EPHEMERAL_SANDBOX,
    "startedAt": "2026-11-10T00:00:00Z",
    "endAt": "2026-11-10T00:15:00Z",
    "cpuCount": 2,
    "memoryMB": 2048,
    "diskSizeMB": 8192,
    "state": "running",
}


class Recorder:
    """an httpx handler that replays queued responses and keeps every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            return httpx.Response(200, json={})
        status, payload = self.responses.pop(0)
        if isinstance(payload, (bytes, str)):
            return httpx.Response(status, content=payload)
        return httpx.Response(status, json=payload)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def body(self, index: int = 0) -> dict:
        return json.loads(self.requests[index].content.decode() or "{}")


class TransportTests(unittest.TestCase):
    def test_version_suffix_is_not_doubled(self):
        self.assertEqual(
            api_root("https://api.machines.internal/v1", "https://api.machines.internal", "/v1"),
            "https://api.machines.internal",
        )
        self.assertEqual(
            api_root("https://api.machines.internal/", "https://api.machines.internal", "/v1"),
            "https://api.machines.internal",
        )
        self.assertEqual(api_root("", "https://api.machines.internal"), "https://api.machines.internal")


class PersistentContractTests(unittest.TestCase):
    def provider(self, recorder: Recorder) -> PersistentProvider:
        return PersistentProvider(
            "key_123",
            "https://api.machines.internal/v1",
            "clawcrew/persistent:latest",
            transport=recorder.transport,
        )

    def test_create_sends_idempotency_key_and_image(self):
        recorder = Recorder((201, PERSISTENT_COMPUTER))
        handle = self.provider(recorder).ensure("crew:a", {"vcpus": 2, "ram_mb": 2048})
        request = recorder.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/v1/computers")
        self.assertEqual(request.headers["idempotency-key"], "crew:a")
        self.assertEqual(request.headers["authorization"], "Bearer key_123")
        self.assertEqual(request.headers["x-persistent-client"], "clawcrew")
        body = recorder.body()
        self.assertEqual(body["image"], "clawcrew/persistent:latest")
        self.assertEqual(body["vcpus"], 2)
        self.assertEqual(body["ram_mb"], 2048)
        self.assertEqual(handle.ref, "cmp_1")
        self.assertEqual(handle.kind, "persistent")
        self.assertEqual(handle.meta["ssh"], "ssh://x")
        self.assertEqual(handle.meta["status"], "running")

    def test_create_without_id_fails_loudly(self):
        recorder = Recorder((201, {"status": "queued", "name": "x"}))
        with self.assertRaises(ProviderFailure):
            self.provider(recorder).ensure("crew:b", {})

    def test_exec_reads_exit_code_and_duration(self):
        recorder = Recorder((200, {"exit_code": 2, "stdout": "out", "stderr": "err", "duration_ms": 41}))
        result = self.provider(recorder).exec(Handle("persistent", "persistent", "cmp_1"), "ls /nope", timeout=30)
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.stdout, "out")
        self.assertEqual(result.duration_ms, 41)
        self.assertEqual(recorder.requests[0].url.path, "/v1/computers/cmp_1/exec")
        self.assertEqual(recorder.body(), {"command": "ls /nope", "timeout": 30})

    def test_timed_out_flag_becomes_exit_124(self):
        recorder = Recorder((200, {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": True}))
        result = self.provider(recorder).exec(Handle("persistent", "persistent", "cmp_1"), "sleep 999")
        self.assertEqual(result.exit_code, 124)

    def test_stop_and_start_hit_their_own_routes(self):
        recorder = Recorder((200, {**PERSISTENT_COMPUTER, "status": "stopped"}), (200, PERSISTENT_COMPUTER))
        provider = self.provider(recorder)
        handle = Handle("persistent", "persistent", "cmp_9")
        provider.stop(handle)
        provider.start(handle)
        self.assertEqual(recorder.requests[0].url.path, "/v1/computers/cmp_9/stop")
        self.assertEqual(recorder.requests[1].url.path, "/v1/computers/cmp_9/start")
        self.assertEqual(recorder.requests[0].method, "POST")

    def test_validation_error_carries_status_and_body(self):
        recorder = Recorder(
            (
                422,
                {
                    "detail": "Validation error",
                    "status_code": 422,
                    "errors": [{"loc": ["body", "vcpus"], "msg": "too many"}],
                },
            )
        )
        with self.assertRaises(ProviderFailure) as ctx:
            self.provider(recorder).ensure("crew:c", {"vcpus": 999})
        self.assertEqual(ctx.exception.details.get("status"), 422)
        self.assertIn("too many", str(ctx.exception.details.get("body")))

    def test_undocumented_status_becomes_provider_failure(self):
        recorder = Recorder((503, "upstream unavailable"))
        with self.assertRaises(ProviderFailure) as ctx:
            self.provider(recorder).ensure("crew:d", {})
        self.assertEqual(ctx.exception.details.get("status"), 503)

    def test_network_policy_string_becomes_object(self):
        spec = self.provider(Recorder()).build_spec({"network_policy": "off", "size_id": "small"})
        self.assertEqual(spec["size_id"], "small")
        self.assertNotIn("name", spec)
        body = spec["network_policy"].to_dict()
        self.assertEqual(body["mode"], "off")

    def test_list_filters_by_status(self):
        stopped = {**PERSISTENT_COMPUTER, "id": "cmp_2", "status": "stopped"}
        recorder = Recorder((200, {"computers": [PERSISTENT_COMPUTER, stopped], "count": 2}))
        rows = self.provider(recorder).list_computers(status="running")
        self.assertEqual([row["id"] for row in rows], ["cmp_1"])

    def test_is_ready_maps_status_words(self):
        running = Recorder((200, PERSISTENT_COMPUTER))
        self.assertTrue(self.provider(running).is_ready(Handle("persistent", "persistent", "cmp_1")))
        stopped = Recorder((200, {**PERSISTENT_COMPUTER, "status": "stopped"}))
        self.assertFalse(self.provider(stopped).is_ready(Handle("persistent", "persistent", "cmp_1")))

    def test_destroy_tolerates_a_gone_computer(self):
        recorder = Recorder((404, {"detail": "not found"}))
        self.provider(recorder).destroy(Handle("persistent", "persistent", "cmp_x"))
        self.assertEqual(recorder.requests[0].method, "DELETE")

    def test_unconfigured_provider_refuses(self):
        with self.assertRaises(ProviderNotConfigured):
            PersistentProvider("").ensure("k", {})


class EphemeralContractTests(unittest.TestCase):
    def provider(self, recorder: Recorder) -> EphemeralProvider:
        return EphemeralProvider("ephemeral_key", "https://api.ephemeral.internal", "base", transport=recorder.transport)

    def test_create_matches_new_sandbox_schema(self):
        recorder = Recorder((201, EPHEMERAL_SANDBOX))
        handle = self.provider(recorder).ensure(
            "task:t1", {"ttl_seconds": 120, "metadata": {"crew": "x"}, "env": {"TOKEN": "1"}}
        )
        request = recorder.requests[0]
        self.assertEqual(request.url.path, "/sandboxes")
        self.assertEqual(request.headers["x-api-key"], "ephemeral_key")
        body = recorder.body()
        self.assertEqual(body["templateID"], "base")
        self.assertEqual(body["timeout"], 120)
        self.assertEqual(body["metadata"]["clawcrew_key"], "task:t1")
        self.assertEqual(body["envVars"], {"TOKEN": "1"})
        self.assertEqual(handle.ref, "sbx_1")
        self.assertEqual(handle.meta["envd_version"], "0.2.0")
        self.assertTrue(handle.meta["has_envd_token"])

    def test_envd_token_is_never_persisted(self):
        recorder = Recorder((201, EPHEMERAL_SANDBOX))
        handle = self.provider(recorder).ensure("task:t2", {})
        self.assertNotIn("secret-token", str(handle.to_dict()))

    def test_pause_resume_connect_and_kill(self):
        recorder = Recorder(
            (204, b""),
            (201, EPHEMERAL_SANDBOX),
            (201, EPHEMERAL_SANDBOX),
            (204, b""),
        )
        provider = self.provider(recorder)
        handle = Handle("ephemeral", "ephemeral", "sbx_1", {"key": "task:t3"})
        provider.pause(handle)
        provider.resume(handle, timeout=60)
        provider.connect(handle, timeout=60)
        provider.destroy(handle)
        paths = [request.url.path for request in recorder.requests]
        self.assertEqual(
            paths,
            [
                "/sandboxes/sbx_1/pause",
                "/sandboxes/sbx_1/resume",
                "/sandboxes/sbx_1/connect",
                "/sandboxes/sbx_1",
            ],
        )
        self.assertEqual(recorder.requests[1].method, "POST")
        self.assertEqual(recorder.requests[3].method, "DELETE")
        self.assertEqual(json.loads(recorder.requests[1].content)["timeout"], 60)

    def test_destroy_tolerates_an_expired_sandbox(self):
        recorder = Recorder((404, {"code": 404, "message": "not found"}))
        self.provider(recorder).destroy(Handle("ephemeral", "ephemeral", "sbx_3"))

    def test_destroy_still_raises_on_real_errors(self):
        recorder = Recorder((500, {"code": 500, "message": "boom"}))
        with self.assertRaises(ProviderFailure):
            self.provider(recorder).destroy(Handle("ephemeral", "ephemeral", "sbx_3"))

    def test_is_ready_reads_the_sandbox_state(self):
        ready = Recorder((200, EPHEMERAL_DETAIL))
        self.assertTrue(self.provider(ready).is_ready(Handle("ephemeral", "ephemeral", "sbx_1")))
        gone = Recorder((404, {"code": 404, "message": "not found"}))
        self.assertFalse(self.provider(gone).is_ready(Handle("ephemeral", "ephemeral", "sbx_1")))

    def test_exec_refuses_without_the_sdk(self):
        provider = self.provider(Recorder())
        self.assertTrue(provider.configured())
        if provider.can_exec():
            self.skipTest("ephemeral sdk is installed in this environment")
        with self.assertRaises(ProviderNotConfigured) as ctx:
            provider.exec(Handle("ephemeral", "ephemeral", "sbx_1"), "ls")
        self.assertIn("envd", ctx.exception.message)
        self.assertEqual(ctx.exception.details.get("hint"), 'pip install "clawcrew[ephemeral]"')


if __name__ == "__main__":
    unittest.main()
