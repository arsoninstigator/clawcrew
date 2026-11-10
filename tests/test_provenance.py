"""identity, provenance, the human reading of a2a, and agent-authored workflows.

four promises under test:
  every agent has one permanent alphanumeric key
  every action it takes lands in its own hash chain, and the chain can be checked
  the same a2a rows read as a sentence for a human and as an A2A envelope for a peer
  an agent can write a workflow, but a human still turns it on
"""

from __future__ import annotations

import re
import unittest

import base  # noqa: F401
from base import AppCase

from clawcrew import a2a_human
from clawcrew import a2a_protocol as spec
from clawcrew.errors import ValidationError


class IdentityTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.a = self.agent(self.team["id"], "wren", role="writer")
        self.b = self.agent(self.team["id"], "okafor", role="editor")

    def test_keys_are_alphanumeric_and_unique(self):
        keys = set()
        for index in range(12):
            agent = self.agent(self.team["id"], f"worker{index}")
            self.assertRegex(agent["agent_key"], r"^AC[A-Z0-9]{10}$")
            keys.add(agent["agent_key"])
        self.assertEqual(len(keys), 12)

    def test_a_key_cannot_be_reassigned(self):
        with self.assertRaises(ValidationError):
            self.crews.update_agent(self.a["id"], agent_key="ACNEWKEY123")
        self.assertEqual(self.crews.by_key(self.a["agent_key"])["id"], self.a["id"])

    def test_scope_defaults_are_the_safe_half(self):
        scope = self.a["scope"]
        self.assertTrue(scope["permissions"]["dispatch_tasks"])
        self.assertFalse(scope["permissions"]["approve_workflows"])
        self.assertFalse(scope["permissions"]["spend"])
        self.assertEqual(scope["data_scope"], "crew")

    def test_scope_updates_merge_instead_of_replacing(self):
        self.crews.update_agent(
            self.a["id"], scope={"permissions": {"approve_workflows": True}, "rules": ["no hype"]}
        )
        scope = self.crews.get_agent(self.a["id"])["scope"]
        self.assertTrue(scope["permissions"]["approve_workflows"])
        self.assertTrue(scope["permissions"]["dispatch_tasks"])
        self.assertEqual(scope["rules"], ["no hype"])


class AuditChainTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.a = self.agent(self.team["id"], "wren", role="writer")
        self.b = self.agent(self.team["id"], "okafor", role="editor")
        self.channel = self.app.channels.create(self.team["id"], "growth", members=[self.a["id"]])
        self.audit = self.app.audit

    def test_a_card_move_lands_in_the_owners_chain(self):
        card = self.task(self.team["id"], "draft", column="in_progress", assignee_agent_id=self.a["id"])
        self.kanban.move(card["id"], "review", actor=self.a["id"])
        history = self.audit.history(self.a["id"])
        actions = [entry["action"] for entry in history]
        self.assertIn("task.moved", actions)
        entry = next(e for e in history if e["action"] == "task.moved")
        self.assertEqual(entry["target_id"], card["id"])
        self.assertEqual(entry["payload"]["column"], "review")

    def test_the_chain_verifies_and_tampering_breaks_it(self):
        for index in range(3):
            self.audit.record(
                team_id=self.team["id"], agent_id=self.a["id"], action="task.moved",
                payload={"index": index},
            )
        self.assertTrue(self.audit.verify(self.a["id"])["ok"])
        with self.app.store.tx() as c:
            c.execute(
                "update agent_audit set payload = ? where agent_id = ? and seq = 2",
                ('{"index":99}', self.a["id"]),
            )
        broken = self.audit.verify(self.a["id"])
        self.assertFalse(broken["ok"])
        self.assertEqual(broken["broken_at_seq"], 2)

    def test_each_agent_has_its_own_chain(self):
        self.audit.record(team_id=self.team["id"], agent_id=self.a["id"], action="task.created")
        self.audit.record(team_id=self.team["id"], agent_id=self.b["id"], action="task.created")
        self.assertEqual(len(self.audit.history(self.a["id"])), 1)
        self.assertEqual(len(self.audit.history(self.b["id"])), 1)
        self.assertEqual(self.audit.history(self.a["id"])[0]["seq"], 1)

    def test_a_room_message_is_attributed_to_its_agent_author_only(self):
        self.app.channels.post(
            self.channel["id"], "picked the angle", author_id=self.a["id"]
        )
        self.app.channels.post(
            self.channel["id"], "looks good", author_kind="human", author_id="owner"
        )
        actions = [e["action"] for e in self.audit.history(self.a["id"])]
        self.assertIn("channel.message", actions)
        self.assertEqual([e for e in self.audit.history(self.b["id"]) if e["action"] == "channel.message"], [])

    def test_an_a2a_message_records_sender_and_recipient(self):
        self.app.a2a.send(
            team_id=self.team["id"], from_agent_id=self.a["id"],
            to_agent_id=self.b["id"], body="here is the brief", kind="handoff",
        )
        entry = self.audit.history(self.a["id"])[0]
        self.assertEqual(entry["action"], "a2a.sent")
        self.assertEqual(entry["target_id"], self.b["id"])
        self.assertEqual(entry["payload"]["kind"], "handoff")


class A2AHumanTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.a = self.agent(self.team["id"], "wren", role="writer")
        self.b = self.agent(self.team["id"], "okafor", role="editor")
        self.card = self.kanban.create_task(self.team["id"], "launch post", column="review")
        self.message = self.app.a2a.send(
            team_id=self.team["id"], from_agent_id=self.a["id"], to_agent_id=self.b["id"],
            body="draft is ready", task_id=self.card["id"], kind="review_request",
        )

    def test_one_row_reads_as_a_sentence_and_as_an_envelope(self):
        agents = a2a_human.agents_index(self.crews, self.team["id"])
        rendered = a2a_human.render_message(
            self.message, agents, card_title="launch post", team_name="shop"
        )
        self.assertIn("wren", rendered["human"])
        self.assertIn("okafor", rendered["human"])
        self.assertIn("asks for review", rendered["human"])
        self.assertIn("launch post", rendered["human"])
        self.assertEqual(rendered["wire"]["role"], spec.ROLE_AGENT)
        self.assertTrue(rendered["wire"]["parts"])
        self.assertEqual(rendered["wire"]["metadata"]["clawcrew"]["agentKey"], self.a["agent_key"])
        self.assertEqual(rendered["wire"]["metadata"]["clawcrew"]["status"], "pending")

    def test_god_view_shows_what_is_waiting_on_the_owner(self):
        view = a2a_human.transcript(self.app.a2a, self.crews, self.team["id"], kanban=self.kanban)
        self.assertEqual(view["counts"]["threads"], 1)
        self.assertEqual(view["counts"]["pending"], 1)
        line = view["threads"][0]["digest"][0]
        self.assertIn("waiting on owner review", line)
        self.assertEqual(view["protocol"]["wire"], "A2A")

    def test_approved_traffic_disappears_from_the_pending_count(self):
        self.app.a2a.review(self.message["id"], "approved", reviewer="owner")
        view = a2a_human.transcript(self.app.a2a, self.crews, self.team["id"], kanban=self.kanban)
        self.assertEqual(view["counts"]["pending"], 0)

    def test_a_thread_renders_as_plain_text(self):
        thread = a2a_human.thread_view(
            self.app.a2a, self.crews, self.message["thread_id"], kanban=self.kanban
        )
        self.assertIn("wren asks for review", thread["as_text"])
        self.assertEqual(len(thread["messages"]), 1)


class WorkflowAuthoringTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.a = self.agent(self.team["id"], "wren", role="writer")
        self.b = self.agent(self.team["id"], "okafor", role="editor")
        self.definition = {
            "nodes": [
                {"id": "seed", "type": "set_context", "key": "digest", "value": "daily"},
            ]
        }

    def propose(self, name: str = "nightly digest"):
        return self.app.workflows.propose(
            self.team["id"], name, self.definition, [self.a["id"], self.b["id"]], note="from the desk"
        )

    def test_a_proposal_is_inert_until_approved(self):
        workflow = self.propose()
        self.assertEqual(workflow["status"], "proposed")
        self.assertEqual(len(workflow["coauthors"]), 2)
        self.assertEqual({c["role"] for c in workflow["coauthors"]}, {"author"})
        with self.assertRaises(ValidationError):
            self.app.workflows.run(workflow["id"])

    def test_approval_then_run(self):
        workflow = self.propose()
        reviewed = self.app.workflows.review(workflow["id"], self.b["id"], "approved", note="ship it")
        self.assertTrue(reviewed["ready_for_approval"])
        approved = self.app.workflows.approve(workflow["id"], actor="owner")
        self.assertEqual(approved["status"], "active")
        run = self.app.workflows.run(workflow["id"])
        self.assertEqual(run["status"], "succeeded")

    def test_a_human_can_still_reject(self):
        workflow = self.propose()
        rejected = self.app.workflows.reject(workflow["id"], actor="owner", note="not yet")
        self.assertEqual(rejected["status"], "draft")
        actions = [e["action"] for e in self.app.audit.history(self.a["id"])]
        self.assertIn("workflow.proposed", actions)
        self.assertIn("workflow.rejected", actions)

    def test_only_coauthors_may_review(self):
        workflow = self.propose()
        outsider = self.agent(self.team["id"], "stranger")
        with self.assertRaises(ValidationError):
            self.app.workflows.review(workflow["id"], outsider["id"], "approved")

    def test_publish_is_what_makes_it_run_itself(self):
        workflow = self.propose()
        with self.assertRaises(ValidationError):
            self.app.workflows.publish(workflow["id"], spec="0 3 * * *")
        self.app.workflows.approve(workflow["id"], actor="owner")
        published = self.app.workflows.publish(workflow["id"], spec="0 3 * * *")
        schedule = published["schedule"]
        self.assertEqual(schedule["kind"], "cron")
        self.assertEqual(schedule["task_template"]["workflow_id"], workflow["id"])
        # and the beat will pick it up: the workflow is active and bound
        self.assertEqual(self.app.workflows.get(workflow["id"])["status"], "active")
        published_again = self.app.workflows.publish(workflow["id"], spec="0 4 * * *")
        self.assertEqual(published_again["schedule"]["id"], schedule["id"])

    def test_a_human_saved_workflow_is_still_active_by_default(self):
        workflow = self.app.workflows.save(self.team["id"], "ad hoc", self.definition)
        self.assertEqual(workflow["status"], "active")
        self.assertEqual(self.app.workflows.run(workflow["id"])["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
