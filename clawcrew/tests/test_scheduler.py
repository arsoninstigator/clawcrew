"""scheduling: cron math, interval schedules, durable queue, worker drain."""

from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone

from base import AppCase

from core.errors import ValidationError
from work.scheduler import CronExpression, InProcBroker


class CronTests(unittest.TestCase):
    def test_quarter_hour_cron_fires_on_the_quarter(self):
        cron = CronExpression("*/15 * * * *")
        start = datetime(2026, 11, 10, 9, 3, tzinfo=timezone.utc).timestamp()
        nxt = cron.next_after(start)
        self.assertEqual(datetime.fromtimestamp(nxt, timezone.utc).minute, 15)

    def test_daily_cron_advances_to_next_day(self):
        cron = CronExpression("30 9 * * *")
        start = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc).timestamp()
        nxt = datetime.fromtimestamp(cron.next_after(start), timezone.utc)
        self.assertEqual((nxt.day, nxt.hour, nxt.minute), (11, 9, 30))

    def test_weekday_cron_respects_dow(self):
        cron = CronExpression("0 8 * * 1")  # monday
        start = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc).timestamp()  # tuesday
        nxt = datetime.fromtimestamp(cron.next_after(start), timezone.utc)
        self.assertEqual(nxt.weekday(), 0)

    def test_matches_rejects_wrong_minute(self):
        cron = CronExpression("0 12 * * *")
        self.assertTrue(cron.matches(datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc).timestamp()))
        self.assertFalse(cron.matches(datetime(2026, 11, 10, 12, 1, tzinfo=timezone.utc).timestamp()))

    def test_bad_specs_raise(self):
        for spec in ["* * * *", "61 * * * *", "*/0 * * * *", "a b c d e"]:
            with self.assertRaises(ValidationError):
                CronExpression(spec)

    def test_lists_and_ranges(self):
        cron = CronExpression("0 9,17 * * 1-5")
        self.assertTrue(cron.matches(datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc).timestamp()))
        self.assertFalse(cron.matches(datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc).timestamp()))  # sunday


class ScheduleTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()
        self.agent(self.team["id"])

    def test_interval_schedule_computes_next_run(self):
        schedule = self.app.scheduler.create(
            self.team["id"], "hourly", "interval", "3600", {"team_id": self.team["id"]}
        )
        self.assertGreater(schedule["next_run_at"], time.time() + 3000)
        self.assertTrue(schedule["enabled"])

    def test_short_interval_rejected(self):
        with self.assertRaises(ValidationError):
            self.app.scheduler.create(self.team["id"], "fast", "interval", "5")

    def test_cron_schedule_created(self):
        schedule = self.app.scheduler.create(self.team["id"], "morning", "cron", "0 9 * * *")
        self.assertEqual(schedule["kind"], "cron")

    def test_tick_is_idle_before_due(self):
        self.app.scheduler.create(self.team["id"], "later", "interval", "600", {"team_id": self.team["id"]})
        self.assertEqual(self.app.scheduler.tick(), [])

    def test_tick_enqueues_and_advances(self):
        self.app.scheduler.create(self.team["id"], "soon", "interval", "60", {"team_id": self.team["id"]})
        jobs = self.app.scheduler.tick(at=time.time() + 120)
        self.assertEqual(len(jobs), 1)
        schedule = self.app.scheduler.list(self.team["id"])[0]
        self.assertGreater(schedule["next_run_at"], time.time() + 120)

    def test_disabled_schedule_is_skipped(self):
        schedule = self.app.scheduler.create(self.team["id"], "off", "interval", "60", {"team_id": self.team["id"]})
        self.app.scheduler.set_enabled(schedule["id"], False)
        self.assertEqual(self.app.scheduler.tick(at=time.time() + 3600), [])

    def test_worker_turns_a_scheduled_job_into_a_run(self):
        self.app.scheduler.create(
            self.team["id"], "job", "interval", "60",
            {"team_id": self.team["id"], "title": "scheduled research", "required_skills": ["research"]},
        )
        self.app.scheduler.tick(at=time.time() + 120)
        results = self.app.worker.process()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        tasks = self.kanban.list_tasks(self.team["id"])
        self.assertEqual(tasks[0]["title"], "scheduled research")
        self.assertTrue(tasks[0]["payload"]["scheduled"])

    def test_worker_runs_workflow_jobs(self):
        workflow = self.app.workflows.save(
            self.team["id"], "simple",
            {"nodes": [{"id": "note", "type": "set_context", "key": "ran", "value": True}]},
        )
        self.app.scheduler.create(
            self.team["id"], "wf", "interval", "60",
            {"kind": "workflow", "workflow_id": workflow["id"]},
        )
        self.app.scheduler.tick(at=time.time() + 120)
        results = self.app.worker.process()
        self.assertTrue(results[0]["ok"])
        self.assertEqual(results[0]["outcome"]["status"], "succeeded")

    def test_queue_claims_a_job_only_once(self):
        broker = self.app.worker.broker
        broker.enqueue("task", {"task_id": "t1"})
        first = broker.dequeue(1)
        second = broker.dequeue(1)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        broker.finish(first[0].id, True)
        self.assertEqual(broker.stats()["done"], 1)

    def test_delayed_job_is_not_ready_yet(self):
        broker = InProcBroker(self.app.store)
        broker.enqueue("task", {"task_id": "t2"}, delay=600)
        self.assertEqual(broker.dequeue(1), [])

    def test_full_job_chain_creates_task_and_run(self):
        task = self.kanban.create_task(self.team["id"], "queued work", column="ready")
        self.app.worker.broker.enqueue("task", {"task_id": task["id"]})
        results = self.app.worker.process()
        self.assertTrue(results[0]["ok"])
        self.assertEqual(self.kanban.get_task(task["id"])["column"], "review")


if __name__ == "__main__":
    unittest.main()
