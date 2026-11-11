"""the front door: an outcome in, a reviewed plan out."""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import Conflict, ValidationError


class AskTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.wren = self.agent(self.team["id"], "wren", role="writer", skills=["writing"])
        self.okafor = self.agent(self.team["id"], "okafor", role="research", skills=["research"])
        self.app.channels.create(self.team["id"], "growth")

    def test_a_sentence_becomes_cards_without_running_anything(self):
        ask = self.app.asks.ask(self.team["id"], "write the launch post and then send the "
                                "announcement by friday")
        self.assertEqual(len(ask["plan"]), 2)
        self.assertEqual(ask["mode"], "plan")
        self.assertEqual(ask["dispatched"], 0)
        cards = self.kanban.list_tasks(self.team["id"])
        self.assertEqual({c["column"] for c in cards}, {"backlog"})
        self.assertEqual(self.app.store.query("select count(*) as n from runs")[0]["n"], 0)

    def test_each_card_is_placed_and_owned(self):
        ask = self.app.asks.ask(self.team["id"], "write the launch post, research competitors")
        titles = [p["title"] for p in ask["plan"]]
        self.assertEqual(len(titles), 2)
        owners = {p["assignee_name"] for p in ask["plan"]}
        self.assertTrue(owners <= {"wren", "okafor"})
        self.assertTrue(all(p["channel"] for p in ask["plan"]))
        self.assertTrue(all(p["done_when"] for p in ask["plan"]))

    def test_the_second_card_waits_for_the_first(self):
        ask = self.app.asks.ask(self.team["id"], "write the post and then send it")
        first, second = ask["plan"]
        self.assertEqual(second["depends_on"], [first["task_id"]])
        dispatched = self.app.asks.dispatch(ask["id"])
        self.assertEqual([d["task_id"] for d in dispatched["dispatched"]], [first["task_id"]])
        self.assertEqual(dispatched["skipped"][0]["task_id"], second["task_id"])

    def test_approve_is_the_second_one_word_step(self):
        ask = self.app.asks.ask(self.team["id"], "write the launch post")
        approved = self.app.asks.approve(ask["id"])
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(len(approved["dispatched"]), 1)
        card = self.kanban.get_task(ask["plan"][0]["task_id"])
        # the crew is in review mode, so the work lands in review, not done
        self.assertIn(card["column"], {"in_progress", "review"})

    def test_an_ask_is_idempotent_when_it_has_a_key(self):
        first = self.app.asks.ask(self.team["id"], "write the launch post", idempotency_key="k1")
        again = self.app.asks.ask(self.team["id"], "write the launch post", idempotency_key="k1")
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["replayed"])
        self.assertEqual(len(self.kanban.list_tasks(self.team["id"])), 1)

    def test_a_crew_with_nobody_to_do_the_work_says_so(self):
        lonely = self.crew("empty")
        ask = self.app.asks.ask(lonely["id"], "write the launch post")
        self.assertIsNone(ask["plan"][0]["assignee"])
        self.assertTrue(any("no agent available" in q for q in ask["questions"]))

    def test_the_plan_is_recorded_in_crew_context(self):
        ask = self.app.asks.ask(self.team["id"], "write the launch post")
        context = self.app.crews.context(self.team["id"])["values"]
        self.assertEqual(context["last_ask"]["ask_id"], ask["id"])
        self.assertEqual(len(context["last_ask"]["plan"]), 1)

    def test_empty_text_is_refused(self):
        with self.assertRaises(ValidationError):
            self.app.asks.ask(self.team["id"], "   ")

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValidationError):
            self.app.asks.ask(self.team["id"], "do something", mode="yolo")

    def test_a_cancelled_ask_cannot_be_approved(self):
        ask = self.app.asks.ask(self.team["id"], "write the launch post")
        with self.app.store.tx() as c:
            c.execute("update asks set status = 'cancelled' where id = ?", (ask["id"],))
        with self.assertRaises(Conflict):
            self.app.asks.approve(ask["id"])

    def test_deadline_words_are_read_not_invented(self):
        ask = self.app.asks.ask(self.team["id"], "write the launch post by friday")
        self.assertEqual(ask["interpretation"]["deadline"], "friday")
        self.assertEqual(ask["plan"][0]["done_when"], "finished by friday")


if __name__ == "__main__":
    unittest.main()
