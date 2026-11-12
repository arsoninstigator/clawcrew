"""polls: wait for the world without writing the waiting loop."""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import ValidationError
from work.poll.service import PollService


class Clock:
    def __init__(self, start: float = 1_000_000.0):
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class PollCase(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.clock = Clock()
        self.polls = PollService(self.app.store, self.app.checks, self.app.scheduler.broker,
                                 clock=self.clock, rng=lambda: 0.5)
        self.team = self.crew()

    def time_box(self) -> dict:
        task = self.kanban.create_task(team_id=self.team["id"], title="deploy", column="in_progress")
        return task


class PollRuleTests(PollCase):
    def test_an_unknown_check_is_refused_before_anything_is_stored(self):
        with self.assertRaises(ValidationError):
            self.polls.start(self.team["id"], "x", check="nope", until={"op": "truthy"})
        self.assertEqual(self.app.store.query("select id from polls"), [])

    def test_a_poll_needs_a_stopping_rule(self):
        with self.assertRaises(ValidationError):
            self.polls.start(self.team["id"], "x", check="task.count",
                             check_args={"team_id": self.team["id"]}, until={})

    def test_a_backoff_below_one_is_a_retry_storm_and_is_refused(self):
        with self.assertRaises(ValidationError):
            self.polls.start(self.team["id"], "x", check="task.count",
                             check_args={"team_id": self.team["id"]},
                             until={"path": "value", "op": "gte", "value": 1}, backoff=0.5)

    def test_a_cap_below_the_first_interval_is_refused(self):
        with self.assertRaises(ValidationError):
            self.polls.start(self.team["id"], "x", check="task.count",
                             check_args={"team_id": self.team["id"]},
                             until={"op": "truthy"}, interval=60, max_interval=10)

    def test_every_comparison_does_what_it_says(self):
        from work.poll.service import evaluate
        result = {"value": 4, "state": "open", "tags": ["a", "b"]}
        cases = [
            ({"path": "value", "op": "eq", "value": 4}, True),
            ({"path": "value", "op": "ne", "value": 4}, False),
            ({"path": "value", "op": "gte", "value": 4}, True),
            ({"path": "value", "op": "gt", "value": 4}, False),
            ({"path": "value", "op": "lte", "value": 3}, False),
            ({"path": "state", "op": "in", "value": ["open", "closed"]}, True),
            ({"path": "tags", "op": "contains", "value": "b"}, True),
            ({"path": "state", "op": "truthy"}, True),
            ({"path": "missing", "op": "exists"}, False),
        ]
        for rule, expected in cases:
            self.assertEqual(evaluate(rule, result), expected, rule)


class PollRunTests(PollCase):
    def test_a_satisfied_rule_stops_the_loop_on_the_first_check(self):
        task = self.time_box()
        poll = self.polls.start(self.team["id"], "wait for review", check="task.column",
                                check_args={"task_id": task["id"]},
                                until={"path": "value", "op": "eq", "value": "in_progress"})
        outcome = self.polls.tick(poll["id"])
        self.assertEqual(outcome["status"], "satisfied")
        self.assertEqual(self.polls.get(poll["id"])["attempts"], 1)

    def test_a_waiting_check_schedules_the_next_attempt_instead_of_sleeping(self):
        task = self.time_box()
        poll = self.polls.start(self.team["id"], "wait for review", check="task.column",
                                check_args={"task_id": task["id"]},
                                until={"path": "value", "op": "eq", "value": "review"},
                                interval=5, backoff=2.0, max_interval=25)
        first = self.polls.tick(poll["id"])
        self.assertEqual(first["status"], "waiting")
        self.assertEqual(first["delay"], 5.0)
        self.clock.advance(5)
        self.assertEqual(self.polls.tick(poll["id"])["delay"], 10.0)
        self.clock.advance(10)
        self.assertEqual(self.polls.tick(poll["id"])["delay"], 20.0)
        self.clock.advance(20)
        capped = self.polls.tick(poll["id"])
        self.assertEqual(capped["delay"], 25.0)
        self.assertEqual(self.polls.get(poll["id"])["attempts"], 4)

    def test_interval_caps_at_the_maximum_and_jitter_stays_inside_it(self):
        poll = self.polls.start(
            self.team["id"], "jitter", check="task.count",
            check_args={"team_id": self.team["id"], "column": "done"},
            until={"path": "value", "op": "gte", "value": 99},
            interval=10, backoff=1.0, max_interval=10, jitter=0.5)
        for _ in range(3):
            outcome = self.polls.tick(poll["id"])
            self.assertLessEqual(outcome["delay"], 15.0)
            self.assertGreaterEqual(outcome["delay"], 5.0)

    def test_the_attempt_limit_is_one_of_the_stopping_rules(self):
        poll = self.polls.start(self.team["id"], "never", check="task.count",
                                check_args={"team_id": self.team["id"], "column": "done"},
                                until={"path": "value", "op": "gte", "value": 99},
                                interval=5, max_attempts=3)
        for _ in range(2):
            self.assertEqual(self.polls.tick(poll["id"])["status"], "waiting")
        final = self.polls.tick(poll["id"])
        self.assertEqual(final["status"], "expired")
        self.assertEqual(final["reason"], "max_attempts")
        self.assertEqual([a["status"] for a in self.polls.attempts(poll["id"])],
                         ["waiting", "waiting", "waiting"])

    def test_the_deadline_stops_it_before_another_check(self):
        poll = self.polls.start(self.team["id"], "deadline", check="task.count",
                                check_args={"team_id": self.team["id"]},
                                until={"path": "value", "op": "gte", "value": 99},
                                interval=30, max_attempts=50, deadline=self.clock() + 10)
        self.assertEqual(self.polls.tick(poll["id"])["status"], "waiting")
        self.clock.advance(11)
        outcome = self.polls.tick(poll["id"])
        self.assertEqual(outcome["status"], "expired")
        self.assertEqual(outcome["reason"], "deadline")
        self.assertEqual(len(self.polls.attempts(poll["id"])), 1)

    def test_the_rule_being_met_later_still_stops_it(self):
        task = self.time_box()
        poll = self.polls.start(self.team["id"], "until review", check="task.column",
                                check_args={"task_id": task["id"]},
                                until={"path": "value", "op": "eq", "value": "review"},
                                interval=5)
        self.polls.tick(poll["id"])
        self.kanban.move(task["id"], "review")
        outcome = self.polls.tick(poll["id"])
        self.assertEqual(outcome["status"], "satisfied")
        self.assertEqual(outcome["result"]["value"], "review")

    def test_a_failing_check_is_an_attempt_not_a_crash(self):
        poll = self.polls.start(self.team["id"], "broken", check="task.column",
                                check_args={"task_id": "task_missing"},
                                until={"path": "value", "op": "eq", "value": "done"},
                                interval=5, max_attempts=2)
        first = self.polls.tick(poll["id"])
        self.assertEqual(first["status"], "waiting")
        self.assertIn("NotFound", first["error"])
        self.assertEqual(self.polls.tick(poll["id"])["status"], "expired")

    def test_a_human_can_cancel_a_waiting_poll_and_cancelling_a_settled_one_is_a_no_op(self):
        poll = self.polls.start(self.team["id"], "cancel me", check="task.count",
                                check_args={"team_id": self.team["id"]},
                                until={"path": "value", "op": "gte", "value": 99},
                                interval=5)
        cancelled = self.polls.cancel(poll["id"], reason="not needed any more")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(self.polls.tick(poll["id"])["skipped"])
        self.assertEqual(self.polls.cancel(poll["id"])["status"], "cancelled")

    def test_the_next_step_runs_as_a_queued_action(self):
        poll = self.polls.start(self.team["id"], "then card", check="task.count",
                                check_args={"team_id": self.team["id"]},
                                until={"path": "value", "op": "gte", "value": 0},
                                on_satisfy={"kind": "card", "payload": {
                                    "team_id": self.team["id"], "title": "the wait ended"}})
        self.polls.tick(poll["id"])
        enqueued = [row["kind"] for row in
                    self.app.store.query("select kind from job_queue where status = 'pending'")]
        self.assertIn("card", enqueued)

    def test_the_worker_runs_a_queued_poll_check(self):
        poll = self.polls.start(self.team["id"], "queued", check="task.count",
                                check_args={"team_id": self.team["id"]},
                                until={"path": "value", "op": "gte", "value": 0})
        outcomes = self.app.worker.process(limit=5)
        self.assertTrue(any(o["ok"] for o in outcomes), outcomes)
        self.assertEqual(self.app.polls.get(poll["id"])["status"], "satisfied")


class PollExternalSystemTests(PollCase):
    def test_an_http_check_digs_one_field_out_of_a_json_body(self):
        seen = {}

        def fetcher(url: str, headers: dict):
            seen["url"] = url
            return {"build": {"status": "passed", "id": 41}}

        from types import SimpleNamespace
        from work.ops import build_checks
        checks = build_checks(SimpleNamespace(kanban=self.kanban, a2a=self.a2a,
                                             store=self.app.store), fetcher=fetcher)
        polls = PollService(self.app.store, checks, self.app.scheduler.broker, clock=self.clock)
        poll = polls.start(self.team["id"], "ci green", check="http.json",
                           check_args={"url": "https://ci.example/api/builds/41",
                                       "path": "build.status"},
                           until={"path": "value", "op": "eq", "value": "passed"},
                           interval=30)
        outcome = polls.tick(poll["id"])
        self.assertEqual(outcome["status"], "satisfied")
        self.assertEqual(seen["url"], "https://ci.example/api/builds/41")

    def test_an_http_check_without_a_url_is_refused(self):
        from types import SimpleNamespace
        from work.ops import build_checks
        checks = build_checks(SimpleNamespace(kanban=self.kanban, a2a=self.a2a,
                                             store=self.app.store))
        polls = PollService(self.app.store, checks, self.app.scheduler.broker, clock=self.clock)
        poll = polls.start(self.team["id"], "no url", check="http.json", check_args={},
                           until={"path": "value", "op": "truthy"}, interval=30)
        self.assertIn("needs a url", polls.tick(poll["id"])["error"])


if __name__ == "__main__":
    unittest.main()
