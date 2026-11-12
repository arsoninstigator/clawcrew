"""the overnight pack: five rules, and the two things it needed to be true.

the pack existed and could not work. `schedule.nightly` fires with no crew in context,
so every rule's `${team_id}` rendered empty and the first job of the night died on
validation; and the fifth rule the spec asks for (re-dispatch what nobody touched) needs a
number condition, while conditions were exact string matches only.
"""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import ValidationError


class ConditionTests(AppCase):
    def matches(self, condition: str, context: dict) -> bool:
        return self.app.triggers._matches(condition, context)

    def test_exact_match_still_means_exact_match(self):
        self.assertTrue(self.matches("status=failed", {"status": "failed"}))
        self.assertFalse(self.matches("status=failed", {"status": "succeeded"}))

    def test_a_number_can_be_compared(self):
        self.assertTrue(self.matches("age_minutes>60", {"age_minutes": 90}))
        self.assertFalse(self.matches("age_minutes>60", {"age_minutes": 60}))
        self.assertTrue(self.matches("age_minutes>=60", {"age_minutes": 60}))
        self.assertTrue(self.matches("retries<=2", {"retries": "1"}))
        self.assertTrue(self.matches("priority<3", {"priority": 2.5}))

    def test_not_equal(self):
        self.assertTrue(self.matches("status!=failed", {"status": "succeeded"}))
        self.assertFalse(self.matches("status!=failed", {"status": "failed"}))

    def test_a_comparison_against_a_word_is_false_rather_than_a_crash(self):
        self.assertFalse(self.matches("status>3", {"status": "failed"}))
        self.assertFalse(self.matches("missing>3", {}))

    def test_several_clauses_all_have_to_hold(self):
        self.assertTrue(self.matches("age_minutes>10,status=ready",
                                     {"age_minutes": 30, "status": "ready"}))
        self.assertFalse(self.matches("age_minutes>10,status=ready",
                                      {"age_minutes": 30, "status": "done"}))

    def test_a_condition_that_is_not_a_condition_is_refused_at_save_time(self):
        team = self.crew("shop")
        with self.assertRaises(ValidationError):
            self.app.triggers.create(team["id"], "bad", event="run.failed",
                                     action="card", payload={}, condition="status is failed")


class PackTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.wren = self.agent(self.team["id"], "wren", role="writing", skills=["writing"])

    def test_the_pack_is_five_rules_and_a_dry_run_writes_none(self):
        dry = self.app.triggers.install_pack(self.team["id"], "overnight", dry_run=True)
        self.assertEqual(len(dry["rules"]), 5)
        self.assertEqual(dry["created"], [])
        self.assertEqual(self.app.triggers.list(self.team["id"]), [])

    def test_adopting_twice_is_a_no_op_by_name(self):
        self.app.triggers.install_pack(self.team["id"], "overnight")
        again = self.app.triggers.install_pack(self.team["id"], "overnight")
        self.assertEqual(again["created"], [])
        self.assertEqual(len(again["skipped"]), 5)
        self.assertEqual(len(self.app.triggers.list(self.team["id"])), 5)

    def test_a_nightly_rule_renders_its_own_crew_even_with_no_crew_in_the_event(self):
        """the bug that made the pack inert: `schedule.nightly` carries no team."""
        self.app.triggers.install_pack(self.team["id"], "overnight")
        fired = self.app.triggers.fire("schedule.nightly", {"team_id": "", "at": 0})
        self.assertTrue(fired, "the nightly event matched no rule")
        for entry in fired:
            self.assertEqual(entry["payload"].get("team_id"), self.team["id"], entry)

    def test_the_idle_rule_carries_its_knobs(self):
        self.app.triggers.install_pack(self.team["id"], "overnight")
        rule = next(r for r in self.app.triggers.list(self.team["id"])
                    if r["name"] == "overnight: re-dispatch idle")
        self.assertEqual(rule["action"], "dispatch")
        self.assertEqual(rule["payload"]["idle_minutes"], 120)


class IdleDispatchTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.wren = self.agent(self.team["id"], "wren", role="writing", skills=["writing"])

    def stale(self, title: str, minutes: int) -> dict:
        task = self.kanban.create_task(self.team["id"], title, column="ready")
        with self.app.store.tx() as c:
            c.execute("update tasks set updated_date = updated_date - ? where id = ?",
                      (minutes * 60, task["id"]))
        return task

    def test_only_the_cards_nobody_touched_get_dispatched(self) -> None:
        old = self.stale("nobody has looked at this", 240)
        fresh = self.stale("someone is on it", 1)
        self.app.triggers.install_pack(self.team["id"], "overnight")
        self.app.triggers.fire("schedule.nightly", {"team_id": ""})
        results = self.app.worker.process(limit=20)
        outcomes = [r.get("outcome") or {} for r in results if r.get("ok")]
        self.assertTrue(any("dispatched" in o for o in outcomes), results)

        self.assertIn(self.kanban.get_task(old["id"])["column"], {"in_progress", "review", "done"})
        self.assertEqual(self.kanban.get_task(fresh["id"])["column"], "ready")

    def test_a_dispatch_job_with_no_target_at_all_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.app.worker._dispatch_idle({})


if __name__ == "__main__":
    unittest.main()
