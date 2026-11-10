"""kanban: transitions, board projection, event log, cross-crew guards."""

from __future__ import annotations

import unittest

from base import AppCase

from clawcrew.errors import NotFound, ValidationError


class TaskTests(AppCase):
    def test_new_task_starts_in_backlog_and_logs_creation(self):
        team = self.crew()
        task = self.kanban.create_task(team["id"], "write the brief")
        self.assertEqual(task["column"], "backlog")
        events = self.kanban.events(task["id"])
        self.assertEqual(events[0]["kind"], "created")

    def test_unknown_column_rejected(self):
        team = self.crew()
        with self.assertRaises(ValidationError):
            self.kanban.create_task(team["id"], "x", column="someday")

    def test_bad_isolation_rejected(self):
        team = self.crew()
        with self.assertRaises(ValidationError):
            self.kanban.create_task(team["id"], "x", isolation="host")

    def test_legal_path_to_done(self):
        team = self.crew()
        task = self.task(team["id"])
        self.kanban.move(task["id"], "in_progress")
        self.kanban.move(task["id"], "review")
        done = self.kanban.move(task["id"], "done")
        self.assertEqual(done["column"], "done")
        kinds = [e["kind"] for e in self.kanban.events(task["id"])]
        self.assertEqual(kinds.count("moved"), 3)

    def test_illegal_jump_is_refused(self):
        team = self.crew()
        task = self.task(team["id"])
        with self.assertRaises(ValidationError) as ctx:
            self.kanban.move(task["id"], "done")
        self.assertIn("illegal transition", ctx.exception.message)

    def test_blocked_card_can_return_to_ready(self):
        team = self.crew()
        task = self.task(team["id"])
        self.kanban.move(task["id"], "blocked")
        self.assertEqual(self.kanban.move(task["id"], "ready")["column"], "ready")

    def test_review_note_only_lands_on_review(self):
        team = self.crew()
        task = self.task(team["id"])
        self.kanban.move(task["id"], "in_progress")
        moved = self.kanban.move(task["id"], "review", note="check the numbers")
        self.assertEqual(moved["review_note"], "check the numbers")

    def test_board_groups_by_column_with_counts(self):
        team = self.crew()
        self.task(team["id"], "one")
        self.kanban.create_task(team["id"], "two")
        board = self.kanban.board(team["id"])
        self.assertEqual([c["name"] for c in board["columns"]][:2], ["backlog", "ready"])
        self.assertEqual(board["counts"]["ready"], 1)
        self.assertEqual(board["counts"]["backlog"], 1)

    def test_assign_requires_same_crew(self):
        first, second = self.crew("a"), self.crew("b")
        task = self.task(first["id"])
        outsider = self.agent(second["id"])
        with self.assertRaises(ValidationError):
            self.kanban.assign(task["id"], outsider["id"])

    def test_assign_and_unassign(self):
        team = self.crew()
        agent = self.agent(team["id"])
        task = self.task(team["id"])
        assigned = self.kanban.assign(task["id"], agent["id"])
        self.assertEqual(assigned["assignee_agent_id"], agent["id"])
        self.assertIsNone(self.kanban.assign(task["id"], None)["assignee_agent_id"])

    def test_complete_stores_result_and_moves(self):
        team = self.crew()
        task = self.task(team["id"])
        self.kanban.move(task["id"], "in_progress")
        done = self.kanban.complete(task["id"], {"answer": 42})
        self.assertEqual(done["column"], "done")
        self.assertEqual(done["result"]["answer"], 42)

    def test_missing_task_raises(self):
        with self.assertRaises(NotFound):
            self.kanban.get_task("task_nope")

    def test_custom_columns_come_from_policy(self):
        team = self.crew("custom", orchestration={"kanban_columns": ["triage", "doing"]})
        self.assertEqual(self.kanban.columns(team["id"]), ["triage", "doing"])


if __name__ == "__main__":
    unittest.main()
