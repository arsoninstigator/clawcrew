"""self improvement: propose, dispose, promote, and the one thing it may not touch."""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import NotAllowed, ValidationError


class LessonCase(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.wren = self.agent(self.team["id"], "wren", role="writer", skills=["writing"])
        self.statement = "read the templates folder before drafting, the tone is set there"

    def failed_run(self) -> str:
        card = self.kanban.create_task(self.team["id"], "draft the post", column="ready")
        from core.db import new_id, now

        run_id = new_id("run")
        with self.app.store.tx() as c:
            c.execute(
                """insert into runs (id, task_id, agent_id, provider, status, error, created_date)
                   values (?,?,?,?,?,?,?)""",
                (run_id, card["id"], self.wren["id"], "local", "failed", "boom", now()),
            )
        return run_id


class ProposalTests(LessonCase):
    def test_a_lesson_starts_as_a_proposal_and_steers_nothing(self) -> None:
        lesson = self.app.lessons.propose(self.wren["id"], self.statement)
        self.assertEqual(lesson["status"], "proposed")
        profile = self.app.profile.profile(self.wren["id"])
        self.assertEqual(profile["namespaces"]["instructions"], {})

    def test_a_one_word_lesson_is_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.app.lessons.propose(self.wren["id"], "careful")

    def test_a_lesson_can_cite_the_run_it_came_from(self) -> None:
        run_id = self.failed_run()
        lesson = self.app.lessons.propose(self.wren["id"], self.statement, run_id=run_id)
        self.assertEqual(lesson["run_id"], run_id)
        self.assertTrue(lesson["task_id"])

    def test_failed_runs_without_a_lesson_are_listed(self) -> None:
        run_id = self.failed_run()
        missing = self.app.lessons.candidates(self.team["id"])
        self.assertEqual([c["run_id"] for c in missing], [run_id])
        self.app.lessons.propose(self.wren["id"], self.statement, run_id=run_id)
        self.assertEqual(self.app.lessons.candidates(self.team["id"]), [])


class PromotionTests(LessonCase):
    def test_promotion_needs_an_approval_first(self) -> None:
        lesson = self.app.lessons.propose(self.wren["id"], self.statement)
        with self.assertRaises(ValidationError):
            self.app.lessons.promote(lesson["id"])

    def test_an_approved_lesson_lands_in_instructions_with_its_origin(self) -> None:
        lesson = self.app.lessons.propose(self.wren["id"], self.statement)
        approved = self.app.lessons.approve(lesson["id"])
        promoted = self.app.lessons.promote(approved["id"])
        self.assertEqual(promoted["status"], "promoted")
        profile = self.app.profile.profile(self.wren["id"])
        self.assertEqual(profile["namespaces"]["instructions"][promoted["write_as"]],
                         self.statement)
        origin = profile["provenance"][f"instructions.{promoted['write_as']}"]["origin"]
        self.assertEqual(origin, f"lesson:{lesson['id']}")

    def test_the_operator_may_rewrite_the_sentence_before_it_lands(self) -> None:
        lesson = self.app.lessons.propose(self.wren["id"], self.statement)
        approved = self.app.lessons.approve(lesson["id"], statement="read templates first, always")
        promoted = self.app.lessons.promote(approved["id"])
        profile = self.app.profile.profile(self.wren["id"])
        self.assertEqual(profile["namespaces"]["instructions"][promoted["write_as"]],
                         "read templates first, always")

    def test_a_lesson_cannot_loosen_a_constraint(self) -> None:
        self.app.intent.record(
            self.team["id"],
            "never publish to the production account without a human approval",
            kind="constraint",
        )
        lesson = self.app.lessons.propose(
            self.wren["id"],
            "the human approval step is no longer needed for the production account, "
            "the checks are enough",
        )
        with self.assertRaises(NotAllowed):
            self.app.lessons.approve(lesson["id"])
        constraints = self.app.intent.active(self.team["id"])
        self.assertTrue(any(c["kind"] == "constraint" for c in constraints))

    def test_rejection_is_recorded_and_final(self) -> None:
        lesson = self.app.lessons.propose(self.wren["id"], self.statement)
        rejected = self.app.lessons.reject(lesson["id"], reason="not true")
        self.assertEqual(rejected["status"], "rejected")
        with self.assertRaises(ValidationError):
            self.app.lessons.approve(lesson["id"])
