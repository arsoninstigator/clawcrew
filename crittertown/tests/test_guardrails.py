"""guardrails: the no that was decided in advance, and the receipt it leaves."""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import NotAllowed, NotFound, ValidationError


class GuardrailCase(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.wren = self.agent(self.team["id"], "wren", role="writer", skills=["writing"])

    def ready(self, title: str = "draft the post") -> dict:
        return self.kanban.create_task(self.team["id"], title, column="ready")


class LimitTests(GuardrailCase):
    def test_a_number_kind_takes_a_number(self) -> None:
        rail = self.app.guardrails.set(self.team["id"], "open_cards", 3)
        self.assertEqual(rail["value"], 3)
        with self.assertRaises(ValidationError):
            self.app.guardrails.set(self.team["id"], "open_cards", "three")
        with self.assertRaises(ValidationError):
            self.app.guardrails.set(self.team["id"], "open_cards", -1)
        with self.assertRaises(ValidationError):
            self.app.guardrails.set(self.team["id"], "not_a_kind", 1)

    def test_a_list_kind_takes_a_list(self) -> None:
        rail = self.app.guardrails.set(self.team["id"], "egress_hosts", ["api.github.com"])
        self.assertEqual(rail["value"], ["api.github.com"])
        with self.assertRaises(ValidationError):
            self.app.guardrails.set(self.team["id"], "egress_hosts", "api.github.com")

    def test_one_live_limit_per_slot(self) -> None:
        first = self.app.guardrails.set(self.team["id"], "open_cards", 3)
        again = self.app.guardrails.set(self.team["id"], "open_cards", 9)
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self.app.guardrails.list(self.team["id"])), 1)

    def test_retiring_removes_it_from_the_question(self) -> None:
        rail = self.app.guardrails.set(self.team["id"], "open_cards", 0)
        self.assertFalse(self.app.guardrails.check(self.team["id"], "dispatch")["allowed"])
        self.app.guardrails.retire(rail["id"])
        self.assertTrue(self.app.guardrails.check(self.team["id"], "dispatch")["allowed"])
        with self.assertRaises(NotFound):
            self.app.guardrails.get("rail_nope")


class RefusalTests(GuardrailCase):
    def test_a_dispatch_over_the_ceiling_is_refused_and_parked(self) -> None:
        self.app.guardrails.set(self.team["id"], "open_cards", 1)
        self.ready("one")
        second = self.ready("two")
        with self.assertRaises(NotAllowed) as caught:
            self.app.runner.dispatch(second["id"])
        self.assertIn("open_cards", str(caught.exception))
        self.assertEqual(self.kanban.get_task(second["id"])["column"], "blocked")

    def test_a_refusal_is_counted_apart_from_an_error(self) -> None:
        self.app.guardrails.set(self.team["id"], "open_cards", 0)
        card = self.ready()
        with self.assertRaises(NotAllowed):
            self.app.runner.dispatch(card["id"])
        summary = self.app.guardrails.summary(self.team["id"])
        self.assertEqual(summary["refused"], {"open_cards": 1})
        self.assertEqual(summary["total"], 1)

    def test_an_allowed_dispatch_still_runs(self) -> None:
        self.app.guardrails.set(self.team["id"], "open_cards", 5)
        card = self.ready()
        outcome = self.app.runner.dispatch(card["id"])
        self.assertIn("run_id", outcome)

    def test_a_forbidden_tool_refuses_the_agent_that_holds_it(self) -> None:
        self.app.crews.update_agent(self.wren["id"], tools=["browser"])
        self.app.guardrails.set(self.team["id"], "forbidden_tools", ["browser"])
        verdict = self.app.guardrails.check(
            self.team["id"], "dispatch", {"agent_id": self.wren["id"], "tools": ["browser"]})
        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["kind"], "forbidden_tools")

    def test_egress_outside_the_allowlist_is_refused(self) -> None:
        self.app.guardrails.set(self.team["id"], "egress_hosts", ["api.github.com"])
        inside = self.app.guardrails.check(
            self.team["id"], "egress", {"host": "api.github.com"})
        self.assertTrue(inside["allowed"])
        outside = self.app.guardrails.check(
            self.team["id"], "egress", {"host": "example.com"})
        self.assertFalse(outside["allowed"])

    def test_an_agent_limit_only_binds_that_agent(self) -> None:
        self.app.guardrails.set(self.team["id"], "parallel_runs", 0, agent_id=self.wren["id"])
        refused = self.app.guardrails.check(
            self.team["id"], "dispatch", {"agent_id": self.wren["id"]})
        self.assertFalse(refused["allowed"])
        other = self.app.guardrails.check(
            self.team["id"], "dispatch", {"agent_id": "agent_someone_else"})
        self.assertTrue(other["allowed"])

    def test_spend_is_not_faked_when_nothing_meters_it(self) -> None:
        self.app.guardrails.set(self.team["id"], "spend_per_day", 100)
        self.assertTrue(self.app.guardrails.check(self.team["id"], "dispatch")["allowed"])
        refused = self.app.guardrails.check(
            self.team["id"], "dispatch", {"spend_today": 120})
        self.assertFalse(refused["allowed"])
