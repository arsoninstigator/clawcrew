"""routines: recurring work with a receipt, which is what a schedule does not have."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from base import AppCase

from core.errors import Conflict, NotFound


def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


class RoutineCase(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.wren = self.agent(self.team["id"], "wren", role="writer", skills=["writing"])
        self.inbox = self.app.routines.create(
            self.team["id"], "check the inbox", description="read and triage",
            cadence="daily", agent_id=self.wren["id"], steps=["open mail", "triage", "reply"],
        )


class RoutineTests(RoutineCase):
    def test_a_routine_materialises_one_card_and_one_receipt(self):
        result = self.app.routines.materialise(self.team["id"], on="2026-11-13")
        self.assertEqual(len(result["created"]), 1)
        receipt = self.app.routines.run_for(self.inbox["id"], "2026-11-13")
        self.assertEqual(receipt["status"], "open")
        card = self.app.kanban.get_task(receipt["task_id"])
        self.assertEqual(card["title"], "routine: check the inbox")
        self.assertIn("1. open mail", card["description"])

    def test_materialising_twice_in_a_day_is_a_no_op(self):
        first = self.app.routines.materialise(self.team["id"], on="2026-11-13")
        second = self.app.routines.materialise(self.team["id"], on="2026-11-13")
        self.assertEqual(len(first["created"]), 1)
        self.assertEqual(second["created"], [])

    def test_a_day_that_passed_without_a_kept_routine_is_missed_once(self):
        self.app.routines.materialise(self.team["id"], on=days_ago(3))
        swept = self.app.routines.materialise(self.team["id"], on=days_ago(0))
        self.assertEqual(len(swept["missed"]), 1)
        receipt = self.app.routines.run_for(self.inbox["id"], days_ago(3))
        self.assertEqual(receipt["status"], "missed")
        # and a second sweep does not rewrite the same day again
        self.app.routines.sweep(self.team["id"], on=days_ago(0))
        self.assertEqual(self.app.routines.run_for(self.inbox["id"], days_ago(3))["status"],
                         "missed")

    def test_completing_a_routine_closes_the_receipt_and_the_card(self):
        self.app.routines.materialise(self.team["id"], on="2026-11-13")
        receipt = self.app.routines.run_for(self.inbox["id"], "2026-11-13")
        done = self.app.routines.complete(self.inbox["id"], on="2026-11-13", notes="quiet day")
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["notes"], "quiet day")
        self.assertEqual(self.app.kanban.get_task(receipt["task_id"])["column"], "done")
        # completing twice is not an error, it is the same answer
        self.assertEqual(self.app.routines.complete(self.inbox["id"], on="2026-11-13")["status"],
                         "done")

    def test_completing_something_that_was_never_due_is_loud(self):
        with self.assertRaises(NotFound):
            self.app.routines.complete(self.inbox["id"], on="2026-11-13")

    def test_cadence_decides_which_days_are_due(self):
        weekly = self.app.routines.create(self.team["id"], "weekly review", cadence="weekly",
                                          weekday="monday")
        self.assertTrue(self.app.routines._is_due({**weekly}, "2026-11-16"))  # a monday
        self.assertFalse(self.app.routines._is_due({**weekly}, "2026-11-17"))
        weekdays = self.app.routines.create(self.team["id"], "standup", cadence="weekdays")
        self.assertTrue(self.app.routines._is_due({**weekdays}, "2026-11-16"))
        self.assertFalse(self.app.routines._is_due({**weekdays}, "2026-11-15"))  # sunday
        monthly = self.app.routines.create(self.team["id"], "close the books",
                                           cadence="monthly")
        self.assertTrue(self.app.routines._is_due({**monthly}, "2026-12-01"))
        self.assertFalse(self.app.routines._is_due({**monthly}, "2026-12-02"))

    def test_two_routines_cannot_share_a_name_in_one_crew(self):
        with self.assertRaises(Conflict):
            self.app.routines.create(self.team["id"], "check the inbox")

    def test_a_paused_routine_is_not_due(self):
        self.app.routines.pause(self.inbox["id"])
        self.assertEqual(self.app.routines.due(self.team["id"], on="2026-11-13"), [])
        self.app.routines.pause(self.inbox["id"], status="active")
        self.assertEqual(len(self.app.routines.due(self.team["id"], on="2026-11-13")), 1)

    def test_the_summary_is_kept_against_missed_not_a_claim(self):
        self.app.routines.materialise(self.team["id"], on=days_ago(2))
        self.app.routines.complete(self.inbox["id"], on=days_ago(2))
        self.app.routines.materialise(self.team["id"], on=days_ago(1))
        self.app.routines.sweep(self.team["id"], on=days_ago(0))
        summary = self.app.routines.summary(self.team["id"], days=7, on=days_ago(0))
        self.assertEqual(summary["kept"], 1)
        self.assertEqual(summary["missed"], 1)
        self.assertEqual(summary["rate"], 0.5)
        self.assertEqual(summary["routines"][0]["name"], "check the inbox")

    def test_the_list_says_whether_today_has_happened(self):
        listing = self.app.routines.list(self.team["id"], on="2026-11-13")
        self.assertTrue(listing[0]["due_today"])
        self.assertIsNone(listing[0]["run"])
        self.app.routines.materialise(self.team["id"], on="2026-11-13")
        listing = self.app.routines.list(self.team["id"], on="2026-11-13")
        self.assertEqual(listing[0]["run"]["status"], "open")


class NightlyTests(RoutineCase):
    def test_the_beat_materialises_the_town_instead_of_leaving_it_to_a_rule(self):
        from ingredients.schedule import jobs

        jobs.app = self.app
        result = jobs.nightly()
        self.assertEqual(len(result["routines"]["crews"]), 1)
        self.assertEqual(len(result["routines"]["crews"][0]["created"]), 1)

    def test_one_broken_crew_does_not_stop_the_others(self):
        other = self.crew("support")
        self.app.routines.create(other["id"], "answer the queue", cadence="daily")
        original = self.app.routines.list

        def flaky(team_id, **kwargs):
            if team_id == self.team["id"]:
                raise NotFound("crew vanished")
            return original(team_id, **kwargs)

        self.app.routines.list = flaky
        result = self.app.routines.materialise_all()
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(len(result["crews"]), 1)


class RoutineRouteTests(RoutineCase):
    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient

        from api.routes import create_api

        self.client = TestClient(create_api(self.app))

    def test_the_routes_round_trip_a_routine(self):
        created = self.client.post(f"/crews/{self.team['id']}/routines",
                                   json={"name": "post the digest", "cadence": "weekly",
                                         "weekday": "friday", "steps": ["draft", "send"]})
        self.assertEqual(created.status_code, 200, created.text)
        listing = self.client.get(f"/crews/{self.team['id']}/routines").json()
        names = [r["name"] for r in listing["routines"]]
        self.assertIn("post the digest", names)
        materialised = self.client.post(
            f"/crews/{self.team['id']}/routines/materialise", params={"on": "2026-11-13"}).json()
        self.assertTrue(materialised["created"])
        routine_id = materialised["created"][0]["routine_id"]
        done = self.client.post(f"/routines/{routine_id}/done",
                                json={"on": "2026-11-13", "notes": "sent"}).json()
        self.assertEqual(done["status"], "done")
