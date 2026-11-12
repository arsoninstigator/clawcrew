"""triggers: when this happens, queue that.

the interesting behaviour is not the table, it is the wiring: a card moving, a
message landing in a room, a workflow finishing and the nightly beat all reach the
same engine, and what it enqueues is a normal job the worker already knows how to
run. these tests follow that path and never call the worker itself.
"""

from __future__ import annotations

import unittest

import base  # noqa: F401
from base import AppCase

from core.errors import ValidationError
from work.triggers import TriggerService, lookup


class TriggerRuleTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.writer = self.agent(self.team["id"], "writer", role="writer")

    def jobs(self) -> list[dict]:
        rows = self.store_rows()
        return rows

    def store_rows(self) -> list[dict]:
        return [
            {"kind": r["kind"], "payload": __import__("json").loads(r["payload"]), "status": r["status"]}
            for r in self.app.store.query("select * from job_queue order by created_date")
        ]

    def test_bad_action_and_bad_condition_fail_at_save_time(self):
        with self.assertRaises(ValidationError):
            self.app.triggers.create(self.team["id"], "x", "task.moved", "teleport")
        with self.assertRaises(ValidationError):
            self.app.triggers.create(self.team["id"], "x", "task.moved", "task", condition="oops")

    def test_moving_a_card_queues_the_action_with_a_rendered_payload(self):
        self.app.triggers.create(
            self.team["id"],
            "dispatch on review",
            event="task.moved",
            action="task",
            payload={"task_id": "${task.id}", "max_turns": 2},
            condition="task.column=review",
        )
        card = self.task(self.team["id"], "draft", column="in_progress")
        self.kanban.move(card["id"], "review", actor="writer")
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["kind"], "task")
        self.assertEqual(jobs[0]["payload"]["task_id"], card["id"])
        self.assertEqual(jobs[0]["payload"]["max_turns"], 2)

    def test_condition_that_does_not_match_queues_nothing(self):
        self.app.triggers.create(
            self.team["id"], "only done", event="task.moved", action="task",
            condition="task.column=done",
        )
        card = self.task(self.team["id"], "draft", column="in_progress")
        self.kanban.move(card["id"], "review", actor="writer")
        self.assertEqual(self.jobs(), [])

    def test_a_disabled_rule_is_skipped(self):
        trigger = self.app.triggers.create(
            self.team["id"], "paused", event="task.moved", action="task",
            payload={"task_id": "${task.id}"},
        )
        self.app.triggers.set_enabled(trigger["id"], False)
        card = self.task(self.team["id"], "draft", column="in_progress")
        self.kanban.move(card["id"], "review", actor="writer")
        self.assertEqual(self.jobs(), [])
        self.assertEqual(self.app.triggers.get(trigger["id"])["fired_count"], 0)

    def test_a_room_message_can_trigger_work(self):
        channel = self.app.channels.create(self.team["id"], "growth", members=[self.writer["id"]])
        self.app.triggers.create(
            self.team["id"], "log messages", event="channel.message", action="workflow",
            payload={"workflow_id": "${channel.slug}", "note": "${message.body}"},
        )
        self.app.channels.post(channel["id"], "we shipped", author_id=self.writer["id"])
        jobs = self.jobs()
        self.assertEqual(jobs[0]["payload"]["workflow_id"], "growth")
        self.assertEqual(jobs[0]["payload"]["note"], "we shipped")

    def test_nightly_event_reaches_every_crew(self):
        self.app.triggers.create(
            self.team["id"], "digest", event="schedule.nightly", action="workflow",
            payload={"workflow_id": "wf_nightly"},
        )
        fired = self.app.triggers.fire("schedule.nightly", {"team_id": "", "at": 0})
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["payload"]["workflow_id"], "wf_nightly")

    def test_a_finished_workflow_emits_its_own_event(self):
        self.app.triggers.create(
            self.team["id"], "after workflow", event="workflow.finished", action="task",
            payload={"task_id": "${run_id}", "status": "${status}"},
        )
        definition = {"nodes": [{"id": "seed", "type": "set_context", "key": "k", "value": "v"}]}
        workflow = self.app.workflows.save(self.team["id"], "nightly", definition)
        run = self.app.workflows.run(workflow["id"])
        self.assertEqual(run["status"], "succeeded")
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["payload"]["task_id"], run["id"])
        self.assertEqual(jobs[0]["payload"]["status"], "succeeded")

    def test_fired_count_and_last_fired_are_recorded(self):
        trigger = self.app.triggers.create(
            self.team["id"], "count me", event="task.moved", action="task",
            payload={"task_id": "${task.id}"},
        )
        card = self.task(self.team["id"], "a", column="in_progress")
        self.kanban.move(card["id"], "review", actor="writer")
        self.kanban.move(card["id"], "in_progress", actor="writer")
        refreshed = self.app.triggers.get(trigger["id"])
        self.assertEqual(refreshed["fired_count"], 2)
        self.assertIsNotNone(refreshed["last_fired_at"])


class TemplateTests(unittest.TestCase):
    def test_lookup_is_forgiving(self):
        context = {"task": {"id": "t1"}, "list": [{"a": 1}]}
        self.assertEqual(lookup(context, "task.id"), "t1")
        self.assertEqual(lookup(context, "list.0.a"), 1)
        self.assertIsNone(lookup(context, "task.nope.deep"))
        self.assertIsNone(lookup({}, "anything"))

    def test_render_keeps_types_and_fills_the_middle(self):
        service = TriggerService.__new__(TriggerService)
        context = {"n": 3, "name": "wren"}
        self.assertEqual(service._render("${n}", context), 3)
        self.assertEqual(service._render("agent ${name} reporting", context), "agent wren reporting")
        self.assertEqual(service._render({"a": ["${name}"]}, context), {"a": ["wren"]})
        self.assertEqual(service._render(7, context), 7)


class ScheduleConfigTests(unittest.TestCase):
    def test_beat_and_tasks_are_registered_when_the_broker_is_installed(self):
        try:
            from ingredients.schedule import jobs
        except Exception:  # pragma: no cover - broker absent
            self.skipTest("the broker is not installed in this environment")
        if jobs.beat is None:
            self.skipTest("the broker is not installed in this environment")
        beat = jobs.beat.conf.beat_schedule
        self.assertIn("clawcrew-tick", beat)
        self.assertIn("clawcrew-nightly", beat)
        self.assertEqual(beat["clawcrew-tick"]["task"], "clawcrew.jobs.tick")
        self.assertEqual(beat["clawcrew-nightly"]["task"], "clawcrew.jobs.nightly")
        registered = set(jobs.beat.tasks)
        for name in ("clawcrew.jobs.run_job", "clawcrew.jobs.tick", "clawcrew.jobs.fire_event"):
            self.assertIn(name, registered)

    def test_the_default_broker_is_in_process_and_needs_no_package(self):
        """the in-process path is the default and is what the suite runs on, so a fresh
        install drains its own queue without installing anything."""
        from ingredients.schedule import service

        settings = type("S", (), {"broker": "inproc"})()
        self.assertEqual(service.make_broker(object(), settings).name, "inproc")


if __name__ == "__main__":
    unittest.main()
