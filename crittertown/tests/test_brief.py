"""the night, as numbers: a brief nobody has to argue with."""

from __future__ import annotations

import time
import unittest

from base import AppCase

from core.db import dumps, new_id, now


class BriefTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.wren = self.agent(self.team["id"], "wren", role="writer", skills=["writing"])

    def night(self, status: str = "failed", error: str = "provider: boom") -> dict:
        """a run in the night window, written straight into the row it would leave."""
        start, _end, _date = self.app.brief.window()
        card = self.kanban.create_task(self.team["id"], "publish the post", column="ready")
        run_id = new_id("run")
        with self.app.store.tx() as c:
            c.execute(
                """insert into runs (id, task_id, agent_id, provider, status, error, created_date)
                   values (?,?,?,?,?,?,?)""",
                (run_id, card["id"], self.wren["id"], "local", status, error, start + 60))
        return card, run_id

    def test_the_window_is_the_night_not_a_rolling_day(self):
        start, end, date = self.app.brief.window("2026-11-10")
        self.assertEqual(date, "2026-11-10")
        self.assertEqual(end - start, 12 * 3600)
        self.assertLess(start, end)

    def test_overnight_numbers_come_from_rows(self):
        self.night()
        self.night(status="succeeded", error="")
        brief = self.app.brief.build(self.team["id"], store=False)
        night = brief["numbers"]["overnight"]
        # the runs were written just now, which is inside tonight's window
        self.assertGreaterEqual(night["runs"], 2)
        self.assertGreaterEqual(night["failed"], 1)
        self.assertTrue(brief["numbers"]["risks"])

    def test_a_stored_brief_keeps_its_own_numbers(self):
        built = self.app.brief.build(self.team["id"], date="2026-11-01")
        self.assertEqual(built["numbers"]["overnight"]["runs"], 0)  # that night was quiet
        self.night()  # work happens later in the week
        stored = self.app.brief.get(self.team["id"], "2026-11-01")
        self.assertEqual(stored["numbers"]["overnight"]["runs"], 0)  # the row did not move
        again = self.app.brief.build(self.team["id"], date="2026-11-01")
        self.assertEqual(again["id"], built["id"])  # upsert, not a second row
        rows = self.app.store.query(
            "select count(*) as n from briefs where team_id = ?", (self.team["id"],))
        self.assertEqual(int(rows[0]["n"]), 1)

    def test_prose_may_say_less_but_never_something_else(self):
        numbers = {"overnight": {"runs": 4, "ok": 3, "failed": 1, "running": 0,
                                 "cards_completed": 2, "cards_moved": 5, "triggers_fired": 1},
                   "waiting": {"review": 1, "blocked": 0, "a2a_pending": 0,
                               "handoffs": 0, "gates": 0},
                   "risks": []}
        self.assertTrue(self.app.brief.consistent(numbers, "3 of 4 overnight runs finished."))
        self.assertFalse(self.app.brief.consistent(numbers, "7 of 4 overnight runs finished."))

    def test_an_empty_crew_gets_zeros_not_an_error(self):
        brief = self.app.brief.build(self.team["id"], store=False)
        self.assertEqual(brief["numbers"]["overnight"]["runs"], 0)
        self.assertIn("nothing ran overnight", brief["prose"])

    def test_the_agent_slice_is_the_same_numbers_narrowed(self):
        _, run_id = self.night()
        self.night(status="succeeded", error="")
        mine = self.app.brief.build(self.team["id"], agent_id=self.wren["id"], store=False)
        self.assertEqual(mine["numbers"]["overnight"]["runs"], 2)
        other = self.agent(self.team["id"], "okafor", role="research")
        theirs = self.app.brief.build(self.team["id"], agent_id=other["id"], store=False)
        self.assertEqual(theirs["numbers"]["overnight"]["runs"], 0)

    def test_the_pack_makes_the_night_do_something(self):
        dry = self.app.triggers.install_pack(self.team["id"], "overnight", dry_run=True)
        self.assertEqual(len(dry["rules"]), 5)
        self.assertEqual(dry["created"], [])
        installed = self.app.triggers.install_pack(self.team["id"], "overnight")
        self.assertEqual(len(installed["created"]), 5)
        again = self.app.triggers.install_pack(self.team["id"], "overnight")
        self.assertEqual(again["created"], [])
        self.assertEqual(len(again["skipped"]), 5)

    def test_a_nightly_brief_job_builds_and_delivers_it(self):
        self.app.triggers.install_pack(self.team["id"], "overnight")
        self.app.channels.create(self.team["id"], "ops")
        self.app.triggers.fire("schedule.nightly", {"team_id": self.team["id"]})
        results = self.app.worker.process(limit=10)
        self.assertTrue(all(r["ok"] for r in results), results)
        rows = self.app.store.query("select count(*) as n from briefs where team_id = ?",
                                    (self.team["id"],))
        self.assertGreaterEqual(int(rows[0]["n"]), 1)
        messages = self.app.channels.messages(
            self.app.channels.list(self.team["id"])[0]["id"])
        self.assertTrue(any("morning brief" in m["body"] for m in messages))

    def test_a_failed_run_becomes_a_card_a_human_can_see(self):
        self.app.triggers.install_pack(self.team["id"], "overnight")
        card, run_id = self.night()
        self.app.runner.events_hook("run.failed", {
            "team_id": self.team["id"], "task_id": card["id"], "run_id": run_id,
            "error": "provider: boom"})
        self.app.worker.process(limit=5)
        review = self.app.kanban.list_tasks(self.team["id"], column="review")
        self.assertTrue(any("provider: boom" in t["title"] for t in review), review)


if __name__ == "__main__":
    unittest.main()
