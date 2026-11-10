"""workflows: dag validation, execution, human gates, branches."""

from __future__ import annotations

import unittest

from base import AppCase

from clawcrew.errors import NotFound, ValidationError
from clawcrew.create.workflow.service import WorkflowService, validate_definition


def linear_dag() -> dict:
    return {
        "nodes": [
            {"id": "setup", "type": "set_context", "key": "goal", "value": "ship v1"},
            {"id": "work", "type": "agent_task", "title": "work on ${goal}", "instruction": "do it",
             "depends_on": ["setup"], "agent": "researcher"},
            {"id": "gate", "type": "human_gate", "title": "approve ${goal}", "depends_on": ["work"]},
            {"id": "publish", "type": "shell", "agent": "researcher",
             "command": "echo published ${goal}", "depends_on": ["gate"]},
        ]
    }


class ValidationTests(AppCase):
    def test_cycle_is_rejected(self):
        bad = {"nodes": [
            {"id": "a", "type": "agent_task", "title": "a", "depends_on": ["b"]},
            {"id": "b", "type": "agent_task", "title": "b", "depends_on": ["a"]},
        ]}
        with self.assertRaises(ValidationError) as ctx:
            validate_definition(bad)
        self.assertIn("cycle", ctx.exception.message)

    def test_dangling_dependency_rejected(self):
        bad = {"nodes": [{"id": "a", "type": "agent_task", "title": "a", "depends_on": ["ghost"]}]}
        with self.assertRaises(ValidationError):
            validate_definition(bad)

    def test_unknown_node_type_rejected(self):
        bad = {"nodes": [{"id": "a", "type": "telepathy"}]}
        with self.assertRaises(ValidationError) as ctx:
            validate_definition(bad)
        allowed = ctx.exception.details["allowed"]
        self.assertEqual(
            allowed,
            ["agent_task", "branch", "dify", "human_gate", "n8n", "set_context", "shell"],
        )

    def test_duplicate_ids_rejected(self):
        bad = {"nodes": [{"id": "a", "type": "set_context", "key": "k", "value": 1},
                         {"id": "a", "type": "set_context", "key": "k2", "value": 2}]}
        with self.assertRaises(ValidationError):
            validate_definition(bad)

    def test_shell_node_needs_command(self):
        with self.assertRaises(ValidationError):
            validate_definition({"nodes": [{"id": "s", "type": "shell", "agent": "x"}]})

    def test_branch_target_must_exist(self):
        bad = {"nodes": [{"id": "b", "type": "branch", "targets": ["ghost"]}]}
        with self.assertRaises(ValidationError):
            validate_definition(bad)

    def test_empty_nodes_rejected(self):
        with self.assertRaises(ValidationError):
            validate_definition({"nodes": []})


class RunTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.agent(self.team["id"], "researcher", skills=["research"])
        self.flows = WorkflowService(self.app.store, self.runner)

    def test_save_bumps_version_on_update(self):
        first = self.flows.save(self.team["id"], "daily", linear_dag())
        second = self.flows.save(self.team["id"], "daily", linear_dag())
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(first["id"], second["id"])

    def test_unknown_workflow_raises(self):
        with self.assertRaises(NotFound):
            self.flows.get("wf_nope")

    def test_run_stops_at_human_gate(self):
        workflow = self.flows.save(self.team["id"], "daily", linear_dag())
        run = self.flows.run(workflow["id"])
        self.assertEqual(run["status"], "waiting")
        self.assertEqual(run["state"]["outputs"]["setup"], "ship v1")
        self.assertIn("work", run["state"]["nodes"])

    def test_resume_after_gate_finishes_the_dag(self):
        workflow = self.flows.save(self.team["id"], "daily", linear_dag())
        run = self.flows.run(workflow["id"])
        resumed = self.flows.resume(run["id"], "gate", "approved", note="looks good")
        self.assertEqual(resumed["status"], "succeeded")
        self.assertIn("published ship v1", resumed["state"]["outputs"]["publish"])

    def test_resume_only_works_when_waiting(self):
        workflow = self.flows.save(self.team["id"], "daily", linear_dag())
        run = self.flows.run(workflow["id"])
        done = self.flows.resume(run["id"], "gate", "approved")
        with self.assertRaises(ValidationError):
            self.flows.resume(done["id"], "gate", "approved")

    def test_gate_creates_a_review_card(self):
        workflow = self.flows.save(self.team["id"], "daily", linear_dag())
        self.flows.run(workflow["id"])
        review_cards = self.kanban.list_tasks(self.team["id"], column="review")
        titles = [c["title"] for c in review_cards]
        # the gate card, plus the agent's own card held for review
        self.assertIn("approve ship v1", titles)
        self.assertEqual(len(review_cards), 2)

    def test_agent_task_node_dispatches_a_real_run(self):
        workflow = self.flows.save(self.team["id"], "daily", linear_dag())
        run = self.flows.run(workflow["id"])
        work_node = run["state"]["nodes"]["work"]
        self.assertEqual(work_node["status"], "succeeded")
        self.assertTrue(work_node["run_id"].startswith("run_"))

    def test_branch_skips_unselected_nodes(self):
        definition = {
            "nodes": [
                {"id": "probe", "type": "shell", "agent": "researcher", "command": "echo ok"},
                {"id": "pick", "type": "branch", "depends_on": ["probe"],
                 "targets": ["yes", "no"],
                 "cases": [{"field": "probe.exit_code", "equals": 0, "target": "yes"}]},
                {"id": "yes", "type": "set_context", "key": "path", "value": "happy", "depends_on": ["pick"]},
                {"id": "no", "type": "set_context", "key": "path", "value": "sad", "depends_on": ["pick"]},
            ]
        }
        workflow = self.flows.save(self.team["id"], "branchy", definition)
        run = self.flows.run(workflow["id"])
        self.assertEqual(run["status"], "succeeded")
        self.assertIn("yes", run["state"]["nodes"])
        self.assertIn("no", run["state"]["skipped"])
        self.assertEqual(self.crews.context(self.team["id"])["values"]["path"], "happy")

    def test_failing_node_marks_the_run_failed(self):
        definition = {"nodes": [{"id": "boom", "type": "agent_task", "title": "x", "agent": "ghost"}]}
        workflow = self.flows.save(self.team["id"], "broken", definition)
        run = self.flows.run(workflow["id"])
        self.assertEqual(run["status"], "failed")
        self.assertIn("not in crew", run["state"]["nodes"]["boom"]["error"])

    def test_trigger_payload_is_addressable(self):
        definition = {"nodes": [
            {"id": "note", "type": "set_context", "key": "campaign", "value": "${trigger.name}"}
        ]}
        workflow = self.flows.save(self.team["id"], "triggered", definition)
        self.flows.run(workflow["id"], {"name": "spring launch"})
        self.assertEqual(
            self.crews.context(self.team["id"])["values"]["campaign"], "spring launch"
        )


if __name__ == "__main__":
    unittest.main()
