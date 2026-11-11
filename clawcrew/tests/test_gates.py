"""approval by link: one tap, no login, exactly once."""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import Conflict, NotAllowed


class GateTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.agent_obj = self.agent(self.team["id"], "wren", role="writer", skills=["writing"])

    def test_links_are_minted_per_decision(self):
        gate = self.app.gates.open(self.team["id"], "ship the pricing page",
                                   prompt="claude wants to publish")
        self.assertEqual(set(gate["links"]), {"approved", "rejected"})
        self.assertTrue(gate["links"]["approved"].startswith("/g/"))

    def test_one_tap_decides_once(self):
        gate = self.app.gates.open(self.team["id"], "ship it")
        token = gate["links"]["approved"].split("/g/")[1]
        preview = self.app.gates.preview(token)
        self.assertTrue(preview["usable"])
        self.assertEqual(preview["decision"], "approved")
        outcome = self.app.gates.decide(token, actor="owner")
        self.assertEqual(outcome["decision"], "approved")
        self.assertFalse(outcome["already"])
        # the same link again is idempotent, not a second decision
        again = self.app.gates.decide(token, actor="owner")
        self.assertTrue(again["already"])
        # the other option is now a conflict, not a silent overwrite
        with self.assertRaises(Conflict):
            self.app.gates.decide(gate["links"]["rejected"].split("/g/")[1])

    def test_a_tampered_link_is_refused(self):
        gate = self.app.gates.open(self.team["id"], "ship it")
        token = gate["links"]["approved"].split("/g/")[1]
        nonce, _, _signature = token.partition(".")
        with self.assertRaises(NotAllowed):
            self.app.gates.preview(f"{nonce}.deadbeef")
        with self.assertRaises(NotAllowed):
            self.app.gates.preview("nonsense")

    def test_expiry_is_a_closed_door_not_a_lost_question(self):
        gate = self.app.gates.open(self.team["id"], "ship it", ttl_hours=0)
        token = gate["links"]["approved"].split("/g/")[1]
        with self.assertRaises(Conflict):
            self.app.gates.decide(token)
        gate_after = self.app.gates.get(gate["id"])
        self.assertEqual(gate_after["status"], "expired")
        self.assertEqual(gate_after["title"], "ship it")

    def test_humans_can_still_decide_over_the_api(self):
        gate = self.app.gates.open(self.team["id"], "ship it")
        outcome = self.app.gates.decide(gate["id"], decision="reject", actor="owner",
                                        note="not this week")
        self.assertEqual(outcome["decision"], "rejected")
        self.assertEqual(self.app.gates.get(gate["id"])["note"], "not this week")

    def test_workflow_gate_parks_the_run_and_the_link_finishes_it(self):
        workflow = self.app.workflows.save(self.team["id"], "publish flow", {"nodes": [
            {"id": "gate", "type": "human_gate", "title": "approve the post"},
        ]})
        run = self.app.workflows.run(workflow["id"])
        self.assertEqual(run["status"], "waiting")
        gate = self.app.gates.pending(run["id"])
        self.assertIsNotNone(gate)
        token = gate["links"]["approved"].split("/g/")[1]
        outcome = self.app.gates.decide(token, actor="owner")
        self.assertEqual(outcome["run"]["status"], "succeeded")
        self.assertEqual(self.app.gates.get(gate["id"])["status"], "decided")

    def test_api_decision_closes_the_open_link(self):
        workflow = self.app.workflows.save(self.team["id"], "flow", {"nodes": [
            {"id": "gate", "type": "human_gate", "title": "ok?"},
        ]})
        run = self.app.workflows.run(workflow["id"])
        gate = self.app.gates.pending(run["id"])
        self.app.workflows.resume(run["id"], "gate", decision="approved")
        self.assertEqual(self.app.gates.get(gate["id"])["decided"], "approved")
        # and the emailed link is spent, so nobody can decide it twice
        with self.assertRaises(Conflict):
            self.app.gates.decide(gate["id"], decision="reject")

    def test_the_decision_is_on_the_record(self):
        gate = self.app.gates.open(self.team["id"], "ship it")
        self.app.gates.decide(gate["links"]["approved"].split("/g/")[1], actor="owner",
                              note="fine by me")
        decided = self.app.gates.list(self.team["id"], status="decided")
        self.assertEqual(len(decided), 1)
        self.assertEqual(decided[0]["decided_by"], "owner")
        self.assertEqual(decided[0]["note"], "fine by me")
        self.assertGreater(decided[0]["decided_date"], 0)


if __name__ == "__main__":
    unittest.main()
