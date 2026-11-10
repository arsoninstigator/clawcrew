"""shifts: coverage arithmetic, and a handoff that carries the work.

the behaviour under test is the one a manager cares about: when the shift changes,
the incoming agent gets the open cards and a brief, and the outgoing agent stops
being the owner. no scheduler required for any of it.
"""

from __future__ import annotations

import unittest

import base  # noqa: F401
from base import AppCase

from clawcrew.db import now
from clawcrew.errors import NotAllowed, ValidationError


class ShiftTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.a = self.agent(self.team["id"], "morning", role="barista")
        self.b = self.agent(self.team["id"], "evening", role="barista")
        self.c = self.agent(self.team["id"], "night", role="barista")
        self.channel = self.app.channels.create(self.team["id"], "bar", members=[self.a["id"]])
        self.shifts = self.app.shifts

    def plan(self, **kwargs):
        kwargs.setdefault("rotation", [self.a["id"], self.b["id"], self.c["id"]])
        kwargs.setdefault("slot_minutes", 60)
        kwargs.setdefault("channel_id", self.channel["id"])
        kwargs.setdefault("anchor_at", now())
        return self.shifts.create_plan(self.team["id"], kwargs.pop("name", "desk"), **kwargs)

    def test_rotation_is_arithmetic(self):
        plan = self.plan(anchor_at=1000.0)
        self.assertEqual(self.shifts.owner_of_slot(plan, 0), self.a["id"])
        self.assertEqual(self.shifts.owner_of_slot(plan, 1), self.b["id"])
        self.assertEqual(self.shifts.owner_of_slot(plan, 2), self.c["id"])
        self.assertEqual(self.shifts.owner_of_slot(plan, 3), self.a["id"])
        self.assertEqual(self.shifts.slot_at(plan, 1000.0 + 3600), 1)
        start, end = self.shifts.slot_window(plan, 2)
        self.assertEqual((start, end), (1000.0 + 7200, 1000.0 + 10800))

    def test_on_duty_reports_the_current_slot(self):
        plan = self.plan(anchor_at=now() - 3600)
        duty = self.shifts.on_duty(plan["id"])
        self.assertEqual(duty["slot_index"], 1)
        self.assertEqual(duty["agent"]["agent_id"], self.b["id"])
        self.assertEqual(duty["on_duty"], [self.b["id"]])

    def test_schedule_lists_upcoming_slots(self):
        plan = self.plan(anchor_at=now() - 3600)
        rows = self.shifts.schedule(plan["id"], slots=3)
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0]["current"])
        self.assertEqual([r["agent_id"] for r in rows], [self.b["id"], self.c["id"], self.a["id"]])

    def test_rolling_is_idempotent_per_slot(self):
        plan = self.plan(anchor_at=now() - 3600)
        first = self.shifts.roll(plan["id"])
        second = self.shifts.roll(plan["id"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.shifts.handoffs(self.team["id"])), 1)

    def test_handoff_carries_the_outgoing_cards_and_accept_moves_them(self):
        plan = self.plan(anchor_at=now() - 3600)  # slot 1: b takes over from a
        card = self.app.channels.create_task(
            self.channel["id"], "close the till", assignee_agent_id=self.a["id"]
        )
        unassigned = self.app.channels.create_task(self.channel["id"], "restock")
        handoff = self.shifts.roll(plan["id"])
        self.assertEqual(handoff["to_agent_id"], self.b["id"])
        self.assertEqual(handoff["from_agent_id"], self.a["id"])
        self.assertEqual(set(handoff["task_ids"]), {card["id"], unassigned["id"]})
        accepted = self.shifts.accept(handoff["id"])
        self.assertEqual(sorted(accepted["transferred"]), sorted([card["id"], unassigned["id"]]))
        self.assertEqual(self.kanban.get_task(card["id"])["assignee_agent_id"], self.b["id"])

    def test_only_the_incoming_agent_takes_the_shift(self):
        plan = self.plan(anchor_at=now() - 3600)
        handoff = self.shifts.roll(plan["id"])
        with self.assertRaises(NotAllowed):
            self.shifts.accept(handoff["id"], agent_id=self.a["id"])

    def test_bundle_says_what_to_do_first(self):
        plan = self.plan(anchor_at=now() - 3600)
        blocked = self.app.channels.create_task(
            self.channel["id"], "vendor is late", column="ready", assignee_agent_id=self.a["id"]
        )
        self.kanban.move(blocked["id"], "blocked", actor=self.a["id"])
        self.app.channels.post(self.channel["id"], "milk delivery is short", author_id=self.a["id"])
        handoff = self.shifts.roll(plan["id"])
        bundle = handoff["bundle"]
        self.assertTrue(any("blocked" in step for step in bundle["next_steps"]))
        self.assertEqual(bundle["blocked"][0]["id"], blocked["id"])
        self.assertEqual(bundle["room"][0]["body"], "milk delivery is short")
        self.assertEqual(bundle["to_agent_id"], self.b["id"])

    def test_sync_mode_shares_the_slot_and_needs_no_acceptance(self):
        plan = self.plan(mode="sync")
        duty = self.shifts.on_duty(plan["id"])
        self.assertEqual(set(duty["on_duty"]), {self.a["id"], self.b["id"], self.c["id"]})
        handoff = self.shifts.roll(plan["id"])
        self.assertEqual(handoff["status"], "accepted")

    def test_decline_keeps_the_shift_open_for_someone_else(self):
        plan = self.plan(anchor_at=now() - 3600)
        handoff = self.shifts.roll(plan["id"])
        declined = self.shifts.decline(handoff["id"], note="off sick")
        self.assertEqual(declined["status"], "declined")
        self.assertEqual(self.shifts.handoffs(self.team["id"], status="open"), [])

    def test_briefing_sees_the_open_handoff(self):
        plan = self.plan(anchor_at=now() - 3600)
        handoff = self.shifts.roll(plan["id"])
        briefing = self.shifts.briefing(self.b["id"])
        self.assertEqual([h["id"] for h in briefing["open_handoffs"]], [handoff["id"]])
        self.assertEqual(briefing["on_shift"][0]["handoff"]["id"], handoff["id"])

    def test_tick_rolls_every_enabled_plan(self):
        self.plan(anchor_at=now() - 3600)
        self.plan(name="support", anchor_at=now() - 60)
        ticked = self.app.tick()
        self.assertEqual(len(ticked["handoffs"]), 2)
        self.assertEqual(self.app.counts()["handoffs"], 2)

    def test_a_rotation_member_must_be_in_the_crew(self):
        outsider_team = self.crew("other")
        outsider = self.agent(outsider_team["id"], "stranger")
        with self.assertRaises(ValidationError):
            self.shifts.create_plan(
                self.team["id"], "bad", [self.a["id"], outsider["id"]], slot_minutes=60
            )

    def test_slot_length_is_bounded(self):
        with self.assertRaises(ValidationError):
            self.shifts.create_plan(self.team["id"], "tiny", [self.a["id"]], slot_minutes=1)


if __name__ == "__main__":
    unittest.main()
