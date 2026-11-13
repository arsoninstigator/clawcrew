"""the three holes an audit found, held shut.

each test here fails on the version before commit `the gate is a table` and passes now.
they are grouped in one file because they are one property: a claim in the docs has to be
a thing the code does.
"""

from __future__ import annotations

import unittest

from base import AppCase

from core.errors import NotAllowed, ValidationError


class ReviewIsAGateTests(AppCase):
    """`done` is only reachable through `review`, and only by someone who may sign it."""

    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.wren = self.agent(self.team["id"], "wren", role="writer", skills=["writing"])
        self.other = self.agent(self.team["id"], "quill", role="editor", skills=["writing"])

    def card(self, column: str = "in_progress") -> dict:
        task = self.kanban.create_task(self.team["id"], "publish the post",
                                       column="ready", assignee_agent_id=self.wren["id"])
        if column == "in_progress":
            return self.kanban.move(task["id"], "in_progress", "owner")
        return task

    def test_there_is_no_edge_from_in_progress_to_done(self) -> None:
        task = self.card()
        with self.assertRaises(ValidationError) as caught:
            self.kanban.move(task["id"], "done", "owner")
        self.assertIn("illegal transition", str(caught.exception))

    def test_an_agent_cannot_sign_off_its_own_work(self) -> None:
        task = self.kanban.move(self.card()["id"], "review", self.wren["id"])
        with self.assertRaises(ValidationError) as caught:
            self.kanban.move(task["id"], "done", self.wren["id"])
        self.assertIn("cannot approve its own work", str(caught.exception))
        self.assertEqual(self.kanban.get_task(task["id"])["column"], "review")

    def test_the_owner_can_sign_it(self) -> None:
        task = self.kanban.move(self.card()["id"], "review", self.wren["id"])
        closed = self.kanban.move(task["id"], "done", "owner")
        self.assertEqual(closed["column"], "done")

    def test_another_agent_can_sign_it(self) -> None:
        task = self.kanban.move(self.card()["id"], "review", self.wren["id"])
        closed = self.kanban.move(task["id"], "done", self.other["id"])
        self.assertEqual(closed["column"], "done")

    def test_an_actor_that_does_not_exist_is_not_a_person(self) -> None:
        task = self.card()
        with self.assertRaises(ValidationError) as caught:
            self.kanban.move(task["id"], "review", "owner-by-assertion")
        self.assertIn("unknown actor", str(caught.exception))

    def test_an_agent_from_another_crew_cannot_move_this_card(self) -> None:
        stranger = self.agent(self.crew("other")["id"], "mole", role="writer")
        task = self.card()
        with self.assertRaises(ValidationError) as caught:
            self.kanban.move(task["id"], "review", stranger["id"])
        self.assertIn("another crew", str(caught.exception))

    def test_completing_as_the_assignee_parks_in_review(self) -> None:
        task = self.card()
        done = self.kanban.complete(task["id"], {"answer": 42}, actor=self.wren["id"])
        self.assertEqual(done["column"], "review")
        self.assertEqual(done["result"]["answer"], 42)

    def test_approve_walks_the_gate_instead_of_jumping_it(self) -> None:
        task = self.card()
        closed = self.kanban.approve(task["id"], "owner")
        self.assertEqual(closed["column"], "done")
        columns = [e["data"]["to"] for e in self.kanban.events(task["id"]) if e["kind"] == "moved"]
        self.assertEqual(columns[-2:], ["review", "done"])


class EgressIsDefaultDenyTests(AppCase):
    """a host is reachable only when a live grant names a scope that matches it."""

    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.wren = self.agent(self.team["id"], "wren", role="writer")
        from core.config import Settings

        self.app.vault.settings = Settings(
            database_path=self.settings.database_path,
            vault_proxy_url="http://agent-vault:14322",
            vault_base_url="http://agent-vault:14321",
        )
        self.app.vault.env = {"WREN_VAULT_TOKEN": "tok_wren_123"}

    def test_a_grant_without_scopes_is_refused_when_it_is_written(self) -> None:
        with self.assertRaises(ValidationError):
            self.app.vault.grant(self.wren["id"], label="github", token_env="WREN_VAULT_TOKEN")

    def test_no_grant_means_no_host(self) -> None:
        verdict = self.app.vault.egress_allowed(self.wren["id"], "github.com")
        self.assertFalse(verdict["allowed"])

    def test_the_proxy_is_in_the_sandbox_env_so_the_check_has_one_door(self) -> None:
        self.app.vault.grant(self.wren["id"], label="github", token_env="WREN_VAULT_TOKEN",
                             scopes=["github.com"])
        env = self.app.vault.sandbox_env(self.wren["id"])
        self.assertEqual(env["HTTPS_PROXY"], "http://agent-vault:14322")
        self.assertEqual(env["HTTP_PROXY"], "http://agent-vault:14322")
        self.assertEqual(env["WREN_VAULT_TOKEN"], "tok_wren_123")


class LocalProviderLanguageTests(unittest.TestCase):
    """one program per command, and never an interpreter."""

    def setUp(self) -> None:
        import tempfile

        from sandbox.local import LocalProvider

        self.root = tempfile.mkdtemp(prefix="guard-")
        self.provider = LocalProvider(root=self.root)
        self.home = "~"

    def refused(self, command: str) -> bool:
        try:
            self.provider._guard(command, self.root)
        except NotAllowed:
            return True
        return False

    def test_the_reproduced_bypass_is_closed(self) -> None:
        self.assertTrue(self.refused("echo hi; rm -rf " + self.home))

    def test_each_composition_is_refused(self) -> None:
        for command in ("ls && whoami", "cat x | sh", "echo `id`", "echo $(id)",
                        "rm -rf " + self.home, "ls > /etc/out.txt", "ls >> ../../x",
                        "cat a > b > c"):
            self.assertTrue(self.refused(command), command)

    def test_one_redirect_into_the_workspace_is_allowed(self) -> None:
        """the guard cannot refuse every redirect and still be usable: writing a file is
        the commonest thing an agent does. exactly one, to a literal path inside."""
        self.assertFalse(self.refused("echo state > memory.txt"))
        self.assertFalse(self.refused("cat a.txt >> log.txt"))
        self.assertTrue(self.refused("echo x > ../outside.txt"))

    def test_interpreters_are_not_on_the_list(self) -> None:
        for command in ("python3 -c 'print(1)'", "bash -lc id", "env ls", "node -e 1"):
            self.assertTrue(self.refused(command), command)

    def test_a_program_that_spawns_another_is_refused(self) -> None:
        self.assertTrue(self.refused("find . -exec rm {} x"))

    def test_paths_outside_the_workspace_are_refused(self) -> None:
        for command in ("cat /etc/hostname", "rm -rf ../../etc", "cat ../secrets"):
            self.assertTrue(self.refused(command), command)

    def test_ordinary_work_inside_the_workspace_is_allowed(self) -> None:
        for command in ("ls -la", "cat notes/today.md", "grep -r needle .",
                        "mkdir build", "rm -rf build"):
            self.assertFalse(self.refused(command), command)

    def test_the_provider_still_runs_an_allowed_command(self) -> None:
        handle = self.provider.ensure("guard", {})
        result = self.provider.exec(handle, "echo hello")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hello", result.stdout)


if __name__ == "__main__":
    unittest.main()
