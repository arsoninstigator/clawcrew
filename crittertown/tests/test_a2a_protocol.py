"""a2a protocol layer: shapes, round trips, and the review gate surviving the wire."""

from __future__ import annotations

import unittest

from base import AppCase

from fastapi.testclient import TestClient

from comms import protocol as spec
from api.routes import create_api
from core.errors import ValidationError


class ShapeTests(unittest.TestCase):
    def test_role_normalisation_accepts_both_spellings(self):
        self.assertEqual(spec.normalize_role("agent"), "agent")
        self.assertEqual(spec.normalize_role("ROLE_USER"), "user")
        self.assertEqual(spec.normalize_role(None), "agent")

    def test_state_normalisation_accepts_both_spellings(self):
        self.assertEqual(spec.normalize_state("working"), "working")
        self.assertEqual(spec.normalize_state("TASK_STATE_COMPLETED"), "completed")

    def test_column_states_cover_the_default_board(self):
        for column in ("backlog", "ready", "in_progress", "review", "blocked", "done"):
            self.assertIn(spec.task_state(column), spec.TASK_STATE_BY_COLUMN.values())
        self.assertEqual(spec.task_state("done"), "completed")
        self.assertEqual(spec.task_state("review"), "input-required")

    def test_parts_flatten_text_data_and_url(self):
        body = spec.from_parts([
            {"text": "first"},
            {"data": {"k": 1}},
            {"url": "https://example.com/x"},
        ])
        self.assertIn("first", body)
        self.assertIn('{"k": 1}', body)
        self.assertIn("https://example.com/x", body)

    def test_empty_parts_rejected(self):
        with self.assertRaises(ValidationError):
            spec.from_parts([])
        with self.assertRaises(ValidationError):
            spec.from_parts([{"text": "  "}])

    def test_raw_bytes_rejected(self):
        with self.assertRaises(ValidationError):
            spec.from_parts([{"raw": "AAA"}])

    def test_message_round_trip_carries_review_metadata(self):
        row = {"id": "msg_1", "team_id": "team_1", "from_agent_id": "agent_1", "to_agent_id": "agent_2",
               "task_id": "task_1", "kind": "handoff", "body": "numbers checked",
               "status": "approved", "reviewed_by": "anne", "thread_id": "thread_9"}
        wire = spec.message_to_json(row)
        self.assertEqual(wire["messageId"], "msg_1")
        self.assertEqual(wire["taskId"], "task_1")
        self.assertEqual(wire["contextId"], "thread_9")
        self.assertEqual(wire["parts"][0]["text"], "numbers checked")
        self.assertEqual(wire["metadata"]["crittertown"]["reviewedBy"], "anne")

        parsed = spec.message_from_json(wire)
        self.assertEqual(parsed["from_agent_id"], "agent_1")
        self.assertEqual(parsed["to_agent_id"], "agent_2")
        self.assertEqual(parsed["body"], "numbers checked")
        self.assertEqual(parsed["kind"], "handoff")

    def test_unattributed_message_is_refused(self):
        with self.assertRaises(ValidationError) as ctx:
            spec.message_from_json({"kind": "message", "role": "ROLE_USER", "parts": [{"text": "hi"}]})
        self.assertIn("agentId", ctx.exception.message)

    def test_task_wire_has_status_artifacts_and_history(self):
        task = {"id": "task_1", "team_id": "team_1", "column": "review", "parent_id": None,
                "assignee_agent_id": "agent_1", "isolation": "agent", "priority": 2,
                "updated_date": 1_760_000_000, "review_note": "check the numbers",
                "result": {"answer": 42}}
        wire = spec.task_to_json(task, [])
        self.assertEqual(wire["id"], "task_1")
        self.assertEqual(wire["status"]["state"], "input-required")
        self.assertTrue(wire["status"]["timestamp"].endswith("Z"))
        self.assertEqual(wire["artifacts"][0]["parts"][0]["data"], {"answer": 42})
        self.assertEqual(wire["metadata"]["crittertown"]["column"], "review")

    def test_agent_card_is_spec_shaped(self):
        crew = {"id": "team_1", "name": "growth crew"}
        agent = {"id": "agent_1", "name": "scout", "role": "research", "skills": ["research", "browser"]}
        card = spec.agent_card(crew, agent, "https://cc.example.com/", "reviewed")
        self.assertEqual(card["name"], "scout")
        self.assertEqual(card["protocolVersion"], spec.PROTOCOL_VERSION)
        self.assertEqual(card["supportedInterfaces"][0]["protocolBinding"], "JSONRPC")
        self.assertEqual(card["supportedInterfaces"][0]["url"], "https://cc.example.com/a2a/rpc")
        self.assertEqual(len(card["skills"]), 2)
        self.assertEqual(card["skills"][0]["tags"], ["research"])
        extension = card["capabilities"]["extensions"][0]
        self.assertEqual(extension["params"]["reviewMode"], "reviewed")

    def test_card_falls_back_to_a_generalist_skill(self):
        card = spec.agent_card({"id": "t", "name": "c"}, {"id": "a", "name": "n", "role": "", "skills": []},
                               "http://x")
        self.assertEqual(card["skills"][0]["tags"], ["generalist"])

    def test_rpc_envelopes(self):
        self.assertEqual(spec.rpc_result(1, {"a": 1})["jsonrpc"], "2.0")
        err = spec.rpc_error(2, spec.RPC_METHOD_NOT_FOUND, "nope")
        self.assertEqual(err["error"]["code"], -32601)


class ConformanceTests(unittest.TestCase):
    """the wire format is checked against the a2a models vendored from the official
    sdk, not against our own idea of the spec."""

    def test_outbound_message_is_a_valid_a2a_message(self):
        row = {"id": "msg_1", "team_id": "team_1", "from_agent_id": "agent_1",
               "body": "numbers checked", "kind": "handoff", "status": "approved"}
        validated = spec.validate_wire("message", spec.message_to_json(row))
        self.assertEqual(validated["role"], "agent")
        self.assertEqual(validated["parts"][0]["text"], "numbers checked")

    def test_outbound_task_is_a_valid_a2a_task(self):
        task = {"id": "task_1", "team_id": "team_1", "column": "review", "result": {"answer": 42}}
        validated = spec.validate_wire("task", spec.task_to_json(task))
        self.assertEqual(validated["status"]["state"], "input-required")
        self.assertEqual(validated["artifacts"][0]["parts"][0]["data"], {"answer": 42})

    def test_agent_card_is_a_valid_a2a_agent_card(self):
        card = spec.agent_card({"id": "t", "name": "c"}, {"id": "a", "name": "scout", "role": "research"},
                               "https://cc.example.com")
        validated = spec.validate_wire("agent_card", card)
        self.assertEqual(validated["name"], "scout")
        self.assertEqual(validated["preferredTransport"], "JSONRPC")
        self.assertEqual(validated["url"], "https://cc.example.com/a2a/rpc")

    def test_a_proto_spelling_is_not_valid_a2a_json(self):
        with self.assertRaises(ValidationError):
            spec.validate_wire("message", {"kind": "message", "messageId": "m", "role": "ROLE_AGENT",
                                           "parts": [{"text": "hi"}]})

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValidationError) as ctx:
            spec.validate_wire("artifact", {})
        self.assertEqual(ctx.exception.details.get("allowed"), ["agent_card", "message", "task"])


class EndpointTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_api(self.app))
        self.crew = self.app.crews.create_team("growth", mission="top of funnel")
        self.scout = self.app.crews.add_agent(self.crew["id"], "scout", role="research", skills=["research"])
        self.writer = self.app.crews.add_agent(self.crew["id"], "writer", role="writing", skills=["writing"])

    def rpc(self, method: str, params: dict, rpc_id: int = 1):
        return self.client.post(
            "/a2a/rpc", json={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
        ).json()

    def test_agent_card_endpoint(self):
        card = self.client.get(f"/agents/{self.scout['id']}/card").json()
        self.assertEqual(card["name"], "scout")
        self.assertEqual(card["metadata"]["crittertown"]["crewId"], self.crew["id"])

    def test_message_send_goes_through_the_review_gate(self):
        result = self.rpc("message/send", {
            "message": {
                "kind": "message",
                "messageId": "msg_in_1",
                "role": "ROLE_USER",
                "parts": [{"text": "the market is 4B"}],
                "metadata": {"crittertown": {"agentId": self.scout["id"], "crewId": self.crew["id"],
                                          "toAgentId": self.writer["id"], "kind": "handoff"}},
            }
        })
        wire = result["result"]
        self.assertEqual(wire["parts"][0]["text"], "the market is 4B")
        self.assertTrue(wire["metadata"]["crittertown"]["reviewPending"])
        # the gate still holds: nothing reached the writer's inbox
        self.assertEqual(self.app.a2a.inbox(self.writer["id"]), [])
        pending = self.app.a2a.review_queue(self.crew["id"])
        self.assertEqual(len(pending), 1)
        self.app.a2a.review(pending[0]["id"], "approved")
        self.assertEqual(len(self.app.a2a.inbox(self.writer["id"])), 1)

    def test_message_send_without_crew_id_is_invalid_params(self):
        error = self.rpc("message/send", {
            "message": {"kind": "message", "role": "ROLE_USER", "parts": [{"text": "x"}],
                        "metadata": {"crittertown": {"agentId": self.scout["id"]}}}
        })["error"]
        self.assertEqual(error["code"], spec.RPC_INVALID_PARAMS)

    def test_tasks_get_returns_a_task_object(self):
        task = self.app.kanban.create_task(self.crew["id"], "collect signals", column="ready")
        result = self.rpc("tasks/get", {"id": task["id"]})
        self.assertEqual(result["result"]["id"], task["id"])
        self.assertEqual(result["result"]["status"]["state"], "submitted")

    def test_tasks_get_missing_task_is_invalid_params(self):
        error = self.rpc("tasks/get", {"id": "task_ghost"})["error"]
        self.assertEqual(error["code"], spec.RPC_INVALID_PARAMS)

    def test_unknown_method(self):
        error = self.rpc("tasks/cancel", {"id": "x"})["error"]
        self.assertEqual(error["code"], spec.RPC_METHOD_NOT_FOUND)

    def test_bad_envelope(self):
        response = self.client.post("/a2a/rpc", json={"id": 1, "method": "message/send"}).json()
        self.assertEqual(response["error"]["code"], spec.RPC_INVALID_REQUEST)


if __name__ == "__main__":
    unittest.main()
