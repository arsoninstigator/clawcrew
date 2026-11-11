"""crews: teams, agents, persistent computers, shared context."""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import Conflict, NotFound, ValidationError


class TeamTests(AppCase):
    def test_create_team_sets_slug_and_policy(self):
        team = self.crew("Growth Crew")
        self.assertEqual(team["slug"], "growth-crew")
        self.assertEqual(team["review_mode"], "reviewed")
        self.assertIn("done", team["orchestration"]["kanban_columns"])

    def test_duplicate_name_is_a_conflict(self):
        self.crew("ops")
        with self.assertRaises(Conflict):
            self.crew("ops")

    def test_bad_review_mode_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.crew("x", review_mode="sometimes")

    def test_update_merges_orchestration(self):
        team = self.crew("ops")
        updated = self.crews.update_team(team["id"], orchestration={"strategy": "round_robin"})
        self.assertEqual(updated["orchestration"]["strategy"], "round_robin")
        self.assertEqual(updated["orchestration"]["decompose"], True)

    def test_missing_team_raises_not_found(self):
        with self.assertRaises(NotFound):
            self.crews.get_team("team_nope")


class AgentTests(AppCase):
    def test_agent_defaults_to_persistent_computer(self):
        team = self.crew()
        agent = self.agent(team["id"])
        self.assertEqual(agent["runtime"]["computer"], "persistent")
        self.assertEqual(agent["runtime"]["provider"], "celesto")
        self.assertEqual(agent["status"], "idle")

    def test_duplicate_agent_name_in_same_crew_conflicts(self):
        team = self.crew()
        self.agent(team["id"], "writer")
        with self.assertRaises(Conflict):
            self.agent(team["id"], "writer")

    def test_same_name_allowed_in_another_crew(self):
        first, second = self.crew("a"), self.crew("b")
        self.agent(first["id"], "writer")
        self.agent(second["id"], "writer")
        self.assertEqual(len(self.crews.list_agents(second["id"])), 1)

    def test_update_agent_serialises_skills(self):
        team = self.crew()
        agent = self.agent(team["id"])
        updated = self.crews.update_agent(agent["id"], skills=["research", "browser"])
        self.assertEqual(updated["skills"], ["research", "browser"])

    def test_bad_status_rejected(self):
        team = self.crew()
        agent = self.agent(team["id"])
        with self.assertRaises(ValidationError):
            self.crews.update_agent(agent["id"], status="sleeping")

    def test_computer_is_provisioned_once_and_reused(self):
        team = self.crew()
        agent = self.agent(team["id"])
        first = self.crews.ensure_computer(agent["id"])
        second = self.crews.ensure_computer(agent["id"])
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["handle"]["ref"], second["handle"]["ref"])
        self.assertEqual(self.crews.get_computer(agent["id"])["state"], "ready")

    def test_computer_missing_returns_none(self):
        team = self.crew()
        agent = self.agent(team["id"])
        self.assertIsNone(self.crews.get_computer(agent["id"]))


class ContextTests(AppCase):
    def test_latest_value_wins_and_history_is_kept(self):
        team = self.crew()
        self.crews.put_context(team["id"], "target", {"arr": 10000})
        self.crews.put_context(team["id"], "target", {"arr": 25000}, author="analyst")
        data = self.crews.context(team["id"])
        self.assertEqual(data["values"]["target"], {"arr": 25000})
        self.assertEqual(len(data["entries"]), 2)
        self.assertEqual(data["entries"][-1]["author"], "analyst")

    def test_empty_key_rejected(self):
        team = self.crew()
        with self.assertRaises(ValidationError):
            self.crews.put_context(team["id"], "", "x")


if __name__ == "__main__":
    unittest.main()
