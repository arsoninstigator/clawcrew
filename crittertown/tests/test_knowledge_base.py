"""the knowledge base: it refuses more than it keeps, on purpose."""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import ValidationError
from manage.knowledge.base import admit


class AdmissionTests(unittest.TestCase):
    def test_a_preference_with_meat_is_kept(self) -> None:
        self.assertTrue(admit(
            "preference", "squash merges",
            "always use squash merges when landing on main, rebase locally")["ok"])

    def test_what_an_agent_already_knows_is_refused(self) -> None:
        verdict = admit("fact", "postgres is a database", "postgres is a database")
        self.assertFalse(verdict["ok"])
        self.assertIn("too short", verdict["reason"])

    def test_a_question_is_a_task(self) -> None:
        verdict = admit("pattern", "what runs the night shift",
                        "which part of the system is responsible for overnight work?")
        self.assertFalse(verdict["ok"])
        self.assertIn("question", verdict["reason"])

    def test_a_decision_needs_its_because(self) -> None:
        self.assertFalse(admit("decision", "sqlite first", "we chose sqlite for the default")["ok"])
        self.assertTrue(admit("decision", "sqlite first",
                              "we chose sqlite because one file with wal is right for one box")["ok"])

    def test_a_failure_mode_needs_a_symptom(self) -> None:
        self.assertFalse(admit("failure_mode", "docker build", "the agent stage fails to copy")["ok"])
        self.assertTrue(admit(
            "failure_mode", "docker build",
            "the agent stage fails to copy because the vendored client is imported",
            symptom="build dies on a missing module that exists in the repo")["ok"])

    def test_a_process_needs_steps(self) -> None:
        self.assertFalse(admit("process", "release", "do the release the usual way")["ok"])
        self.assertTrue(admit(
            "process", "release", "run make check\nbump the version\ntag and push")["ok"])

    def test_transient_state_is_refused(self) -> None:
        verdict = admit("fact", "the port", "the api is on port 8000 currently, it may move")
        self.assertFalse(verdict["ok"])
        self.assertIn("transient", verdict["reason"])


class BaseTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.wren = self.agent(self.team["id"], "wren", role="writer", skills=["writing"])

    def keep(self, kind: str = "preference", title: str = "squash merges",
             body: str = "always use squash merges when landing on main") -> dict:
        return self.app.knowledge.record(self.team["id"], kind, title, body)

    def test_a_kept_entry_is_searchable(self) -> None:
        entry = self.keep()
        self.assertEqual(entry["status"], "active")
        found = self.app.knowledge.search(self.team["id"], "squash")
        self.assertEqual([f["id"] for f in found], [entry["id"]])
        self.assertEqual(self.app.knowledge.search(self.team["id"], "kubernetes"), [])

    def test_a_duplicate_title_is_refused_rather_than_stacked(self) -> None:
        self.keep()
        with self.assertRaises(ValidationError) as caught:
            self.keep(body="always squash merges before landing")
        self.assertIn("already in the base", str(caught.exception))

    def test_updating_goes_through_the_same_admission(self) -> None:
        entry = self.keep()
        with self.assertRaises(ValidationError):
            self.app.knowledge.update(entry["id"], body="squash")
        self.assertEqual(self.app.knowledge.get(entry["id"])["body"],
                         "always use squash merges when landing on main")

    def test_a_retired_entry_leaves_the_prompt(self) -> None:
        entry = self.keep()
        self.assertIn("squash merges", self.app.knowledge.for_prompt(self.team["id"]))
        self.app.knowledge.retire(entry["id"])
        self.assertEqual(self.app.knowledge.for_prompt(self.team["id"]), "")
        self.assertEqual(self.app.knowledge.stats(self.team["id"])["total"], 0)

    def test_the_prompt_block_stays_small(self) -> None:
        for i in range(12):
            self.keep(title=f"preference {i}", body=f"keep doing thing number {i} this way always")
        block = self.app.knowledge.for_prompt(self.team["id"], limit=6)
        self.assertLessEqual(len([l for l in block.splitlines() if l.startswith("  [")]), 6)

    def test_an_agent_specific_entry_only_reaches_that_agent(self) -> None:
        other = self.agent(self.team["id"], "bo", role="researcher", skills=["research"])
        self.app.knowledge.record(
            self.team["id"], "fact", "wren's editor", "wren uses the long form editor",
            agent_id=self.wren["id"])
        self.assertIn("long form editor",
                      self.app.knowledge.for_prompt(self.team["id"], self.wren["id"]))
        self.assertNotIn("long form editor",
                         self.app.knowledge.for_prompt(self.team["id"], other["id"]))
