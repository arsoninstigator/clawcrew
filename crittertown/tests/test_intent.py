"""intent: the sentence above the card, and the reason it is versioned."""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import NotFound, ValidationError


class IntentCase(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop", mission="sell the first hundred")
        self.wren = self.agent(self.team["id"], "wren", role="writer", skills=["writing"])


class IntentTests(IntentCase):
    def test_an_active_intent_supersedes_the_one_of_its_kind(self):
        first = self.app.intent.record(self.team["id"], "publish two posts a week")
        second = self.app.intent.record(self.team["id"], "publish one good post a week")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["supersedes"], first["id"])
        self.assertEqual(self.app.intent.get(first["id"])["status"], "superseded")
        live = self.app.intent.active(self.team["id"])
        self.assertEqual([i["statement"] for i in live], ["publish one good post a week"])

    def test_constraints_accumulate_instead_of_replacing_each_other(self):
        self.app.intent.record(self.team["id"], "never publish without a link check",
                               kind="constraint")
        self.app.intent.record(self.team["id"], "no outbound email without approval",
                               kind="constraint")
        live = self.app.intent.active(self.team["id"])
        constraints = [i for i in live if i["kind"] == "constraint"]
        self.assertEqual(len(constraints), 2)

    def test_a_proposal_steers_nothing_until_it_is_approved(self):
        proposal = self.app.intent.propose(self.team["id"], "the crew should post less",
                                           rationale="engagement fell")
        self.assertEqual(proposal["status"], "proposed")
        self.assertNotIn("the crew should post less", self.app.intent.prompt(self.team["id"]))
        approved = self.app.intent.approve(proposal["id"], edited="post less, and better")
        self.assertEqual(approved["status"], "active")
        self.assertEqual(approved["approved_from"], proposal["id"])
        self.assertIn("post less, and better", self.app.intent.prompt(self.team["id"]))

    def test_a_rejected_proposal_is_kept_with_its_reason(self):
        proposal = self.app.intent.propose(self.team["id"], "hire three agents")
        rejected = self.app.intent.reject(proposal["id"], reason="not this quarter")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["rationale"], "not this quarter")
        self.assertEqual(self.app.intent.active(self.team["id"]), [])

    def test_an_agent_intent_specialises_its_crew_and_only_for_its_kind(self):
        self.app.intent.record(self.team["id"], "ships weekly", kind="direction")
        self.app.intent.record(self.team["id"], "no swearing", kind="constraint")
        self.app.intent.record(self.team["id"], "writes in plain english", kind="direction",
                               agent_id=self.wren["id"])
        live = self.app.intent.active(self.team["id"], self.wren["id"])
        by_kind = {i["kind"]: i["statement"] for i in live}
        self.assertEqual(by_kind["direction"], "writes in plain english")
        self.assertEqual(by_kind["constraint"], "no swearing")

    def test_an_empty_intent_is_a_refusal_not_an_empty_row(self):
        with self.assertRaises(ValidationError):
            self.app.intent.record(self.team["id"], "   ")
        with self.assertRaises(ValidationError):
            self.app.intent.record(self.team["id"], "ok", kind="vibes")

    def test_the_prompt_is_empty_when_the_crew_has_said_nothing(self):
        self.assertEqual(self.app.intent.prompt(self.team["id"]), "")

    def test_history_keeps_the_versions_a_run_was_judged_against(self):
        self.app.intent.record(self.team["id"], "v1")
        self.app.intent.record(self.team["id"], "v2")
        self.app.intent.record(self.team["id"], "v3")
        history = self.app.intent.history(self.team["id"])
        self.assertEqual([h["version"] for h in history], [3, 2, 1])
        self.assertEqual([h["status"] for h in history], ["active", "superseded", "superseded"])


class IntentRouteTests(IntentCase):
    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient

        from api.routes import create_api

        self.client = TestClient(create_api(self.app))

    def test_the_route_round_trips_an_intent(self):
        posted = self.client.post(f"/crews/{self.team['id']}/intent",
                                  json={"statement": "one good post a week",
                                        "kind": "direction"})
        self.assertEqual(posted.status_code, 200, posted.text)
        body = self.client.get(f"/crews/{self.team['id']}/intent").json()
        self.assertTrue(body["kinds"]["direction"])
        self.assertEqual(body["active"][0]["statement"], "one good post a week")

    def test_a_proposal_shows_up_for_approval_and_not_in_the_live_set(self):
        proposal = self.app.intent.propose(self.team["id"], "post daily")
        body = self.client.get(f"/crews/{self.team['id']}/intent").json()
        self.assertEqual([p["id"] for p in body["proposed"]], [proposal["id"]])
        self.assertEqual(body["active"], [])
        approved = self.client.post(f"/intent/{proposal['id']}/approve", json={}).json()
        self.assertEqual(approved["status"], "active")


class IntentInRunTests(IntentCase):
    def test_a_run_reads_the_intent_above_the_task(self):
        self.app.intent.record(self.team["id"], "one good post a week, never three")
        seen: list[str] = []
        act = self.app.runner.model.act

        def capture(system: str, prompt: str):
            seen.append(prompt)
            return act(system, prompt)

        self.app.runner.model.act = capture
        card = self.kanban.create_task(self.team["id"], "draft the post", column="ready")
        self.app.runner.dispatch(card["id"])
        self.assertTrue(seen, "the run produced no prompt")
        prompt = seen[0]
        self.assertIn("one good post a week, never three", prompt)
        self.assertLess(prompt.index("outranks the task"), prompt.index("task: draft the post"))
