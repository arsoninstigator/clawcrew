"""the manage lane: what a crew sends to the board, and what it does with a no."""

import json
import unittest

import base  # noqa: F401  (puts the repo root on sys.path)
import httpx

from core.errors import ProviderFailure, ProviderNotConfigured
from ingredients.projectmanagement.manage.client import make
from ingredients.projectmanagement.manage.bridge import ManageBridge


def client(handler):
    return make("http://board.test", "agent-key", transport=httpx.MockTransport(handler))


class ManageClientTests(unittest.TestCase):
    def test_it_refuses_to_guess_without_a_key(self):
        with self.assertRaises(ProviderNotConfigured):
            make("http://board.test", "").claim()

    def test_status_says_what_is_missing(self):
        status = make("http://board.test", "").status()
        self.assertFalse(status["configured"])
        self.assertIn("CLAWCREW_MANAGE_API_KEY", status["needs"])

    def test_claim_returns_the_run(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["auth"] = request.headers["authorization"]
            return httpx.Response(200, json={"run": {"id": 7, "issueIdentifier": "MKT-42"}})

        run = client(handler).claim()
        self.assertEqual(run["id"], 7)
        self.assertEqual(seen["path"], "/agent-runs/claim")
        self.assertEqual(seen["auth"], "Bearer agent-key")

    def test_an_empty_queue_is_not_an_error(self):
        self.assertIsNone(client(lambda r: httpx.Response(204)).claim())

    def test_a_409_is_someone_else_and_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(409, json={"error": "in flight"})

        self.assertIsNone(client(handler).claim())
        self.assertEqual(calls, ["/agent-runs/claim"])

    def test_heartbeat_and_events_and_result_use_the_run_routes(self):
        paths = []

        def handler(request):
            paths.append((request.method, request.url.path))
            return httpx.Response(200, json={})

        c = client(handler)
        c.heartbeat(7, note="working")
        c.events(7, [{"kind": "note", "text": "jwt done"}])
        c.result(7, "succeeded", "shipped", {"files": ["a.py"]})
        self.assertEqual(paths, [
            ("POST", "/agent-runs/7/heartbeat"),
            ("POST", "/agent-runs/7/events"),
            ("POST", "/agent-runs/7/result"),
        ])

    def test_issues_are_unwrapped(self):
        def handler(request):
            return httpx.Response(200, json={"issues": [{"id": 1, "identifier": "MKT-1"}]})

        self.assertEqual(client(handler).issues("MKT")[0]["identifier"], "MKT-1")

    def test_creating_an_issue_puts_the_assignee_on_it(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.read().decode())
            return httpx.Response(201, json={"id": 9})

        client(handler).create_issue("MKT", "ship it", "with tests", assignee_id=12)
        self.assertEqual(seen["path"], "/projects/MKT/issues")
        self.assertEqual(seen["body"]["assigneeId"], 12)
        self.assertEqual(seen["body"]["title"], "ship it")

    def test_a_missing_route_is_a_version_problem_not_a_silence(self):
        with self.assertRaises(ProviderFailure) as ctx:
            client(lambda r: httpx.Response(404)).claim()
        self.assertIn("not on this instance", str(ctx.exception))


class BridgeTests(unittest.TestCase):
    def test_a_successful_turn_reports_succeeded(self):
        posted = {}
        claims = [{"id": 7, "issueIdentifier": "MKT-42"}]

        def handler(request):
            if request.url.path == "/agent-runs/claim":
                return httpx.Response(200, json={"run": claims.pop()})
            if request.url.path.endswith("/result"):
                posted.update(httpx.Response(200, json={}).json() or {"ok": True})
                posted["body"] = json.loads(request.read().decode())
                return httpx.Response(200, json={})
            return httpx.Response(200, json={})

        bridge = ManageBridge(client=client(handler),
                                executor=lambda run: {"status": "succeeded", "summary": "did MKT-42"})
        outcomes = bridge.poll()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["status"], "succeeded")
        self.assertEqual(posted["body"]["summary"], "did MKT-42")

    def test_a_turn_that_raises_still_closes_the_run(self):
        bodies = []

        def handler(request):
            if request.url.path == "/agent-runs/claim":
                return httpx.Response(200, json={"run": {"id": 8, "issueIdentifier": "MKT-9"}})
            if request.url.path.endswith("/result"):
                bodies.append(json.loads(request.read().decode()))
            return httpx.Response(200, json={})

        def explode(run):
            raise RuntimeError("the model fell over")

        outcomes = ManageBridge(client=client(handler), executor=explode, max_runs=1).poll()
        self.assertEqual(outcomes[0]["status"], "failed")
        self.assertEqual(bodies[0]["status"], "failed")

    def test_an_empty_queue_stops_the_poll(self):
        def handler(request):
            return httpx.Response(204) if request.url.path == "/agent-runs/claim" else httpx.Response(200, json={})

        self.assertEqual(ManageBridge(client=client(handler), executor=lambda r: {}, max_runs=5).poll(), [])


if __name__ == "__main__":
    unittest.main()
