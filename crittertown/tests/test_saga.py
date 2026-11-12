"""sagas: a sequence with a way back."""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import Conflict, ValidationError


class SagaDefinitionTests(AppCase):
    def test_a_step_without_an_explicit_undo_borrows_the_ops_own(self):
        team = self.crew()
        saga = self.app.sagas.create(team["id"], "implicit", [
            {"name": "card", "do": {"op": "task.create",
                                    "args": {"team_id": team["id"], "title": "x"}}},
        ])
        self.assertEqual(saga["steps"][0]["undo"], {"op": "task.create", "args": {}})

    def test_an_op_with_no_undo_of_its_own_is_refused_unless_it_is_irreversible(self):
        from work.ops import Op, OpRegistry
        from work.saga.service import SagaService

        def nothing(args: dict) -> dict:
            return {"ok": True}

        registry = OpRegistry([
            Op("bare.do", nothing, None),
            Op("bare.forever", nothing, None, irreversible=True,
               note="cannot be put back, and says so"),
        ])
        service = SagaService(self.app.store, registry)
        team = self.crew()
        with self.assertRaises(ValidationError) as caught:
            service.create(team["id"], "bad", [
                {"name": "x", "do": {"op": "bare.do", "args": {}}}])
        self.assertIn("has none", caught.exception.message)
        allowed = service.create(team["id"], "ok", [
            {"name": "x", "do": {"op": "bare.forever", "args": {}}}])
        self.assertIsNone(allowed["steps"][0]["undo"])
        outcome = service.run(allowed["id"])
        self.assertEqual(outcome["status"], "done")

    def test_unknown_ops_and_duplicate_names_are_refused(self):
        team = self.crew()
        with self.assertRaises(ValidationError):
            self.app.sagas.create(team["id"], "x", [
                {"name": "a", "do": {"op": "nope", "args": {}}, "undo": {"op": "task.move"}}])
        with self.assertRaises(ValidationError):
            self.app.sagas.create(team["id"], "x", [
                {"name": "same", "do": {"op": "task.create", "args": {}},
                 "undo": {"op": "task.move"}},
                {"name": "same", "do": {"op": "task.create", "args": {}},
                 "undo": {"op": "task.move"}},
            ])

    def test_an_empty_saga_is_not_a_saga(self):
        team = self.crew()
        with self.assertRaises(ValidationError):
            self.app.sagas.create(team["id"], "nothing", [])

    def test_a_reference_to_a_step_that_does_not_exist_fails_loudly(self):
        team = self.crew()
        saga = self.app.sagas.create(team["id"], "typo", [
            {"name": "one", "do": {"op": "task.create",
                                   "args": {"team_id": team["id"], "title": "a"}},
             "undo": {"op": "task.move", "args": {"task_id": "${result.task_id}",
                                                  "column": "ready"}}},
            {"name": "two", "do": {"op": "task.move",
                                   "args": {"task_id": "${steps.typo.task_id}",
                                            "column": "ready"}},
             "undo": {"op": "task.move", "args": {"task_id": "${result.task_id}",
                                                  "column": "backlog"}}},
        ])
        outcome = self.app.sagas.run(saga["id"])
        self.assertEqual(outcome["status"], "reverted")
        self.assertIn("unknown reference", outcome["error"])


class SagaRunTests(AppCase):
    def test_every_step_runs_in_order_and_the_results_are_recorded(self):
        team = self.crew()
        saga = self.app.sagas.create(team["id"], "two cards", [
            {"name": "first", "do": {"op": "task.create",
                                     "args": {"team_id": team["id"], "title": "one"}},
             "undo": {"op": "task.move", "args": {"task_id": "${result.task_id}",
                                                  "column": "ready"}}},
            {"name": "second", "do": {"op": "task.create",
                                      "args": {"team_id": team["id"], "title": "two"}},
             "undo": {"op": "task.move", "args": {"task_id": "${result.task_id}",
                                                  "column": "ready"}}},
        ])
        outcome = self.app.sagas.run(saga["id"])
        self.assertEqual(outcome["status"], "done")
        self.assertEqual([s["status"] for s in outcome["progress"]], ["done", "done"])
        titles = {task["title"] for task in self.kanban.list_tasks(team["id"])}
        self.assertEqual(titles, {"one", "two"})

    def test_a_later_failure_reverses_completed_steps_in_reverse_order(self):
        team = self.crew()
        channel = self.app.channels.create(team["id"], "general")
        saga = self.app.sagas.create(team["id"], "post then break", [
            {"name": "one", "do": {"op": "channel.post",
                                   "args": {"channel_id": channel["id"], "text": "first"}}},
            {"name": "two", "do": {"op": "channel.post",
                                   "args": {"channel_id": channel["id"], "text": "second"}}},
            {"name": "break", "do": {"op": "task.move",
                                     "args": {"task_id": "task_missing", "column": "ready"}},
             "undo": {"op": "task.move", "args": {"task_id": "${result.task_id}",
                                                  "column": "backlog"}}},
        ])
        outcome = self.app.sagas.run(saga["id"])
        self.assertEqual(outcome["status"], "reverted")
        self.assertEqual([s["status"] for s in outcome["progress"]],
                         ["compensated", "compensated", "failed"])
        # reversed: the newest completed step is undone first
        messages = sorted(self.app.channels.messages(channel["id"]),
                          key=lambda m: m["created_date"])
        texts = [m["body"] for m in messages]
        retractions = [t for t in texts if t.startswith("retracted by saga:")]
        self.assertEqual(len(retractions), 2)
        message_ids = [s["result"]["message_id"] for s in outcome["progress"][:2]]
        self.assertEqual([r.split(": ")[1] for r in retractions],
                         [message_ids[1], message_ids[0]])

    def test_a_failed_undo_is_reported_instead_of_hidden(self):
        team = self.crew()
        saga = self.app.sagas.create(team["id"], "bad undo", [
            {"name": "card", "do": {"op": "task.create",
                                    "args": {"team_id": team["id"], "title": "x"}},
             "undo": {"op": "task.move", "args": {"task_id": "${result.task_id}",
                                                  "column": "cancelled"}}},
            {"name": "break", "do": {"op": "task.move",
                                     "args": {"task_id": "task_missing", "column": "ready"}},
             "undo": {"op": "task.move", "args": {"task_id": "${result.task_id}",
                                                  "column": "backlog"}}},
        ])
        outcome = self.app.sagas.run(saga["id"])
        self.assertEqual(outcome["status"], "needs_attention")
        self.assertEqual([s["status"] for s in outcome["progress"]],
                         ["compensation_failed", "failed"])
        self.assertIn("undo(s) failed", outcome["error"])

    def test_running_a_settled_saga_does_nothing_again(self):
        team = self.crew()
        saga = self.app.sagas.create(team["id"], "once", [
            {"name": "card", "do": {"op": "task.create",
                                    "args": {"team_id": team["id"], "title": "only"}},
             "undo": {"op": "task.move", "args": {"task_id": "${result.task_id}",
                                                  "column": "ready"}}},
        ])
        self.app.sagas.run(saga["id"])
        again = self.app.sagas.run(saga["id"])
        self.assertEqual(again["status"], "done")
        self.assertEqual(len(self.kanban.list_tasks(team["id"])), 1)

    def test_a_reverted_saga_can_be_retried_and_the_conflict_is_explicit(self):
        team = self.crew()
        saga = self.app.sagas.create(team["id"], "retry me", [
            {"name": "card", "do": {"op": "task.create",
                                    "args": {"team_id": team["id"], "title": "x"}},
             "undo": {"op": "task.move", "args": {"task_id": "${result.task_id}",
                                                  "column": "ready"}}},
            {"name": "break", "do": {"op": "task.move",
                                     "args": {"task_id": "task_missing", "column": "ready"}},
             "undo": {"op": "task.move", "args": {"task_id": "${result.task_id}",
                                                  "column": "backlog"}}},
        ])
        self.assertEqual(self.app.sagas.run(saga["id"])["status"], "reverted")
        self.assertEqual(self.app.sagas.retry_failed(saga["id"])["status"], "reverted")
        done = self.app.sagas.create(team["id"], "fine", [
            {"name": "card", "do": {"op": "task.create",
                                    "args": {"team_id": team["id"], "title": "y"}},
             "undo": {"op": "task.move", "args": {"task_id": "${result.task_id}",
                                                  "column": "ready"}}},
        ])
        self.app.sagas.run(done["id"])
        with self.assertRaises(Conflict):
            self.app.sagas.retry_failed(done["id"])

    def test_the_worker_runs_a_queued_saga(self):
        team = self.crew()
        saga = self.app.sagas.create(team["id"], "queued", [
            {"name": "card", "do": {"op": "task.create",
                                    "args": {"team_id": team["id"], "title": "queued card"}},
             "undo": {"op": "task.move", "args": {"task_id": "${result.task_id}",
                                                  "column": "ready"}}},
        ])
        self.app.scheduler.broker.enqueue("saga", {"saga_id": saga["id"]})
        outcomes = self.app.worker.process(limit=5)
        self.assertTrue(any(o["ok"] for o in outcomes))
        self.assertEqual(self.app.sagas.get(saga["id"])["status"], "done")

    def test_the_undo_of_a_card_does_not_erase_the_card(self):
        team = self.crew()
        saga = self.app.sagas.create(team["id"], "soft undo", [
            {"name": "card", "do": {"op": "task.create",
                                    "args": {"team_id": team["id"], "title": "keep the trail"}},
             "undo": {"op": "task.create", "args": {}}},
            {"name": "break", "do": {"op": "task.move",
                                     "args": {"task_id": "task_missing", "column": "ready"}},
             "undo": {"op": "task.move", "args": {"task_id": "${result.task_id}",
                                                  "column": "backlog"}}},
        ])
        outcome = self.app.sagas.run(saga["id"])
        self.assertEqual(outcome["status"], "reverted")
        task_id = outcome["progress"][0]["result"]["task_id"]
        self.assertEqual(self.app.kanban.get_task(task_id)["column"], "backlog")
        self.assertEqual(outcome["progress"][0]["status"], "compensated")


if __name__ == "__main__":
    unittest.main()
