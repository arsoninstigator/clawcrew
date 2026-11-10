"""orchestrator: dispatch, agent selection, sandbox lifecycle, review handoff."""

from __future__ import annotations

import unittest

from base import AppCase

from clawcrew.errors import NotFound, ValidationError
from clawcrew.create.agents.runtime import DeterministicModel, parse_action


class SelectionTests(AppCase):
    def test_skill_match_beats_arrival_order(self):
        team = self.crew()
        self.crews.add_agent(team["id"], "writer", role="copywriting", skills=["writing"])
        analyst = self.crews.add_agent(team["id"], "analyst", role="research", skills=["research"])
        task = self.task(team["id"], "research the market", required_skills=["research"])
        self.assertEqual(self.runner.pick_agent(team["id"], task)["id"], analyst["id"])

    def test_explicit_assignee_wins(self):
        team = self.crew()
        writer = self.crews.add_agent(team["id"], "writer", skills=["writing"])
        self.crews.add_agent(team["id"], "analyst", skills=["research"])
        task = self.task(team["id"], "research", required_skills=["research"], assignee_agent_id=writer["id"])
        self.assertEqual(self.runner.pick_agent(team["id"], task)["id"], writer["id"])

    def test_retired_agents_are_skipped(self):
        team = self.crew()
        retired = self.crews.add_agent(team["id"], "old", skills=["research"])
        self.crews.update_agent(retired["id"], status="retired")
        active = self.crews.add_agent(team["id"], "new", skills=["research"])
        task = self.task(team["id"], "research")
        self.assertEqual(self.runner.pick_agent(team["id"], task)["id"], active["id"])

    def test_empty_crew_is_a_validation_error(self):
        team = self.crew()
        task = self.task(team["id"], "anything")
        with self.assertRaises(ValidationError):
            self.runner.pick_agent(team["id"], task)

    def test_assignee_from_other_crew_is_not_found(self):
        team = self.crew()
        other = self.crew("other")
        outsider = self.agent(other["id"])
        task = self.task(team["id"], "x", assignee_agent_id=outsider["id"])
        with self.assertRaises(NotFound):
            self.runner.pick_agent(team["id"], task)


class DispatchTests(AppCase):
    def test_dispatch_runs_turns_and_files_for_review(self):
        team = self.crew()
        self.agent(team["id"])
        task = self.task(team["id"], "inspect the workspace")
        outcome = self.runner.dispatch(task["id"])
        self.assertEqual(outcome["task"]["column"], "review")
        self.assertEqual(len(outcome["steps"]), 2)
        self.assertEqual(outcome["steps"][0]["steps"][0]["command"], "pwd")
        self.assertIn("deterministic", outcome["result"]["summary"])

    def test_open_crew_sends_card_straight_to_done(self):
        team = self.crews.create_team("open-crew", review_mode="open")
        self.agent(team["id"])
        task = self.task(team["id"], "quick job")
        outcome = self.runner.dispatch(task["id"])
        self.assertEqual(outcome["task"]["column"], "done")

    def test_dispatch_from_backlog_is_refused(self):
        team = self.crew()
        self.agent(team["id"])
        task = self.kanban.create_task(team["id"], "not ready")
        with self.assertRaises(ValidationError):
            self.runner.dispatch(task["id"])

    def test_run_record_persists_steps_and_agent(self):
        team = self.crew()
        agent = self.agent(team["id"])
        task = self.task(team["id"], "job")
        outcome = self.runner.dispatch(task["id"])
        run = self.runner.get_run(outcome["run_id"])
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["agent_id"], agent["id"])
        self.assertEqual(len(run["steps"]), 2)
        self.assertTrue(run["ended_at"])

    def test_agent_returns_to_idle_after_run(self):
        team = self.crew()
        agent = self.agent(team["id"])
        task = self.task(team["id"], "job")
        self.runner.dispatch(task["id"])
        self.assertEqual(self.crews.get_agent(agent["id"])["status"], "idle")

    def test_persistent_computer_survives_and_keeps_files(self):
        team = self.crew()
        agent = self.agent(team["id"])
        model = DeterministicModel(
            script=[
                {"thought": "write a file", "commands": ["echo state > memory.txt"], "done": False},
                {"thought": "done", "commands": [], "done": True, "result": {"ok": True}},
            ]
        )
        self.runner.model = model
        first = self.task(team["id"], "remember something")
        self.runner.dispatch(first["id"])

        self.runner.model = DeterministicModel(
            script=[{"thought": "read it back", "commands": ["cat memory.txt"], "done": True, "result": {"ok": True}}]
        )
        second = self.task(team["id"], "recall something")
        outcome = self.runner.dispatch(second["id"])
        command_step = outcome["steps"][0]["steps"][0]
        self.assertIn("state", command_step["stdout"])

    def test_ephemeral_isolation_uses_throwaway_sandbox(self):
        team = self.crew()
        self.agent(team["id"])
        task = self.task(team["id"], "risky job", isolation="ephemeral")
        outcome = self.runner.dispatch(task["id"])
        self.assertIn("cc-eph-", outcome["sandbox"])
        self.assertFalse(__import__("os").path.isdir(outcome["sandbox"]))

    def test_result_lands_in_shared_context(self):
        team = self.crew()
        self.agent(team["id"])
        task = self.task(team["id"], "job")
        self.runner.dispatch(task["id"])
        context = self.crews.context(team["id"])["values"]
        self.assertIn(f"task:{task['id']}:result", context)

    def test_dispatch_ready_runs_every_ready_card(self):
        team = self.crew()
        self.agent(team["id"])
        self.task(team["id"], "one")
        self.task(team["id"], "two")
        self.kanban.create_task(team["id"], "not ready")
        outcomes = self.runner.dispatch_ready(team["id"])
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(self.kanban.board(team["id"])["counts"]["review"], 2)

    def test_agent_turn_can_post_a_message_through_the_gate(self):
        team = self.crew()
        agent = self.agent(team["id"])
        task = self.task(team["id"], "job")
        self.runner.dispatch(task["id"])
        messages = self.a2a.log(team["id"])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["from_agent_id"], agent["id"])
        self.assertEqual(messages[0]["status"], "pending")

    def test_bad_recipient_name_fails_the_run(self):
        team = self.crew()
        self.agent(team["id"])
        self.runner.model = DeterministicModel(
            script=[{"thought": "hand off", "commands": [],
                     "message": {"to": "nobody", "kind": "handoff", "body": "hi"}, "done": True}]
        )
        task = self.task(team["id"], "job")
        with self.assertRaises(ValidationError):
            self.runner.dispatch(task["id"])
        self.assertEqual(self.kanban.get_task(task["id"])["column"], "blocked")


class PlanTests(AppCase):
    def test_declared_subtasks_become_ready_cards(self):
        team = self.crew()
        parent = self.kanban.create_task(
            team["id"], "launch the page",
            payload={"subtasks": [{"title": "write copy", "required_skills": ["writing"]},
                                  {"title": "check seo", "required_skills": ["research"]}]},
        )
        created = self.runner.plan(parent["id"])
        self.assertEqual([c["title"] for c in created], ["write copy", "check seo"])
        self.assertTrue(all(c["parent_id"] == parent["id"] for c in created))
        self.assertTrue(all(c["column"] == "ready" for c in created))

    def test_task_without_subtasks_plans_nothing(self):
        team = self.crew()
        task = self.task(team["id"], "solo")
        self.assertEqual(self.runner.plan(task["id"]), [])


class ActionParsingTests(unittest.TestCase):
    def test_fenced_json_is_parsed(self):
        action = parse_action('```json\n{"thought":"go","commands":["ls"],"done":true}\n```')
        self.assertEqual(action["commands"], ["ls"])
        self.assertTrue(action["done"])

    def test_prose_around_json_is_tolerated(self):
        action = parse_action('sure thing: {"commands": ["pwd"], "done": false} done')
        self.assertEqual(action["commands"], ["pwd"])

    def test_non_json_yields_safe_action(self):
        action = parse_action("no idea")
        self.assertEqual(action["commands"], [])
        self.assertFalse(action["done"])

    def test_commands_are_capped_at_three(self):
        action = parse_action('{"commands":["a","b","c","d","e"]}')
        self.assertEqual(len(action["commands"]), 3)

    def test_string_command_becomes_list(self):
        action = parse_action('{"commands":"ls -la"}')
        self.assertEqual(action["commands"], ["ls -la"])


if __name__ == "__main__":
    unittest.main()
