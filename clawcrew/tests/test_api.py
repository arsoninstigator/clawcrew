"""http surface: status mapping, the happy path, and the review gate over the api."""

from __future__ import annotations

import unittest

from base import AppCase

from fastapi.testclient import TestClient

from api.routes import create_api


class ApiTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_api(self.app))

    def make_crew(self):
        crew = self.client.post("/crews", json={"name": "growth", "mission": "own the funnel"}).json()
        agent = self.client.post(
            f"/crews/{crew['id']}/agents",
            json={"name": "researcher", "role": "research", "skills": ["research"]},
        ).json()
        return crew, agent

    def test_health_reports_counts_and_providers(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["counts"]["crews"], 0)
        self.assertEqual(
            {p["name"] for p in data["providers"]},
            {"celesto-local", "celesto", "e2b", "local"},
        )

    def test_unknown_crew_is_404(self):
        response = self.client.get("/crews/team_missing")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "not_found")

    def test_duplicate_crew_is_409(self):
        self.client.post("/crews", json={"name": "ops"})
        response = self.client.post("/crews", json={"name": "ops"})
        self.assertEqual(response.status_code, 409)

    def test_validation_error_is_422(self):
        response = self.client.post("/crews", json={"name": "!!!"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"], "validation_error")

    def test_full_task_lifecycle(self):
        crew, agent = self.make_crew()
        task = self.client.post(
            f"/crews/{crew['id']}/tasks",
            json={"title": "research rivals", "column": "ready", "required_skills": ["research"]},
        ).json()
        dispatched = self.client.post(f"/tasks/{task['id']}/dispatch", json={})
        self.assertEqual(dispatched.status_code, 200)
        self.assertEqual(dispatched.json()["task"]["column"], "review")
        detail = self.client.get(f"/tasks/{task['id']}").json()
        self.assertEqual(detail["column"], "review")
        self.assertEqual(len(detail["runs"]), 1)
        self.assertGreaterEqual(len(detail["events"]), 3)

    def test_illegal_move_is_422(self):
        crew, _ = self.make_crew()
        task = self.client.post(f"/crews/{crew['id']}/tasks", json={"title": "x", "column": "ready"}).json()
        response = self.client.post(f"/tasks/{task['id']}/move", json={"column": "done"})
        self.assertEqual(response.status_code, 422)

    def test_a2a_review_roundtrip_over_http(self):
        crew, agent = self.make_crew()
        writer = self.client.post(
            f"/crews/{crew['id']}/agents", json={"name": "writer", "role": "writing"}
        ).json()
        message = self.client.post(
            "/a2a",
            json={"team_id": crew["id"], "from_agent_id": agent["id"],
                  "to_agent_id": writer["id"], "body": "the market is 4B", "kind": "handoff"},
        ).json()
        self.assertEqual(message["status"], "pending")
        self.assertEqual(self.client.get(f"/agents/{writer['id']}/inbox").json()["messages"], [])
        queue = self.client.get(f"/crews/{crew['id']}/a2a/review-queue").json()["pending"]
        self.assertEqual(len(queue), 1)
        reviewed = self.client.post(f"/a2a/{message['id']}/review", json={"decision": "approved"}).json()
        self.assertEqual(reviewed["status"], "approved")
        inbox = self.client.get(f"/agents/{writer['id']}/inbox").json()["messages"]
        self.assertEqual(inbox[0]["body"], "the market is 4B")
        second = self.client.post(f"/a2a/{message['id']}/review", json={"decision": "approved"})
        self.assertEqual(second.status_code, 403)

    def test_board_endpoint(self):
        crew, _ = self.make_crew()
        self.client.post(f"/crews/{crew['id']}/tasks", json={"title": "a"})
        board = self.client.get(f"/crews/{crew['id']}/board").json()
        self.assertEqual(board["counts"]["backlog"], 1)

    def test_workflow_endpoint_roundtrip(self):
        crew, _ = self.make_crew()
        definition = {"nodes": [{"id": "n", "type": "set_context", "key": "k", "value": "v"}]}
        saved = self.client.post(
            f"/crews/{crew['id']}/workflows", json={"name": "flow", "definition": definition}
        ).json()
        run = self.client.post(f"/workflows/{saved['id']}/run", json={}).json()
        self.assertEqual(run["status"], "succeeded")
        fetched = self.client.get(f"/workflow-runs/{run['id']}").json()
        self.assertEqual(fetched["state"]["outputs"], {"n": "v"})

    def test_bad_workflow_is_422(self):
        crew, _ = self.make_crew()
        response = self.client.post(
            f"/crews/{crew['id']}/workflows",
            json={"name": "bad", "definition": {"nodes": [{"id": "a", "type": "magic"}]}},
        )
        self.assertEqual(response.status_code, 422)

    def test_context_put_and_get(self):
        crew, _ = self.make_crew()
        self.client.put(f"/crews/{crew['id']}/context", json={"key": "icp", "value": ["founders"]})
        data = self.client.get(f"/crews/{crew['id']}/context").json()
        self.assertEqual(data["values"]["icp"], ["founders"])

    def test_computer_endpoint_provisions_once(self):
        crew, agent = self.make_crew()
        first = self.client.post(f"/agents/{agent['id']}/computer", json={}).json()
        second = self.client.post(f"/agents/{agent['id']}/computer", json={}).json()
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(self.client.get(f"/agents/{agent['id']}/computer").json()["state"], "ready")

    def test_schedule_endpoint_and_tick(self):
        crew, _ = self.make_crew()
        schedule = self.client.post(
            f"/crews/{crew['id']}/schedules",
            json={"name": "hourly", "kind": "interval", "spec": "3600", "task_template": {"team_id": crew["id"]}},
        ).json()
        self.assertTrue(schedule["enabled"])
        tick = self.client.post("/ops/tick").json()
        self.assertEqual(tick["enqueued"], [])
        self.assertEqual(self.client.post("/ops/process").json()["results"], [])

    def test_bad_schedule_is_422(self):
        crew, _ = self.make_crew()
        response = self.client.post(
            f"/crews/{crew['id']}/schedules", json={"name": "x", "kind": "cron", "spec": "nope"}
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()


class AuthTests(AppCase):
    def test_token_is_required_when_configured(self):
        from api.routes import create_api

        self.settings.api_token = "s3cret"
        client = TestClient(create_api(self.app))
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.get("/crews").status_code, 401)
        self.assertEqual(client.get("/crews", headers={"authorization": "Bearer s3cret"}).status_code, 200)
        self.assertEqual(client.get("/crews", headers={"x-clawcrew-token": "s3cret"}).status_code, 200)
        self.assertEqual(client.get("/crews", headers={"authorization": "Bearer wrong"}).status_code, 401)

    def test_open_when_no_token_configured(self):
        from api.routes import create_api

        self.assertEqual(TestClient(create_api(self.app)).get("/crews").status_code, 200)
