"""the celesto OSS runtime, driven through its CLI.

the contract under test is celesto's own: `--json` envelopes shaped
`{ok, command, exit_code, data, error}`, documented command names, and a named
computer that persists between tasks. the CLI process is faked, so these tests pin
the exact argv and the envelope handling without needing virtualization in ci.
"""

from __future__ import annotations

import base64
import json
import unittest

import base  # noqa: F401

from clawcrew.errors import ProviderFailure, ProviderNotConfigured
from clawcrew.sandbox import celesto_local
from clawcrew.sandbox.base import Handle


def envelope(data=None, ok=True, exit_code=0, error=None) -> tuple[int, str, str]:
    payload = {"ok": ok, "command": "computer", "exit_code": exit_code, "data": data or {}}
    if error is not None:
        payload["error"] = error
    return (0 if ok else 1, json.dumps(payload), "")


class FakeCLI:
    """records argv and replays queued (returncode, stdout, stderr) tuples."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[list[str], int]] = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if not self.responses:
            return envelope({})
        return self.responses.pop(0)

    @property
    def argv(self, index: int = 0) -> list[str]:
        return self.calls[index][0]


class CelestoLocalTests(unittest.TestCase):
    def provider(self, cli: FakeCLI) -> celesto_local.CelestoLocalProvider:
        return celesto_local.make("celesto", runner=cli)

    def test_slug_is_stable_and_safe(self):
        self.assertEqual(celesto_local.slug("crew:alpha/1"), "cc-crew-alpha-1")
        self.assertEqual(celesto_local.slug("agent:writer"), "cc-agent-writer")
        self.assertEqual(celesto_local.slug(""), "cc-computer")
        self.assertLessEqual(len(celesto_local.slug("x" * 200)), 63)

    def test_ensure_creates_a_named_computer(self):
        cli = FakeCLI(envelope({"name": "cc-crew-a", "status": "running"}))
        handle = self.provider(cli).ensure("crew:a", {})
        self.assertEqual(handle.ref, "cc-crew-a")
        self.assertEqual(handle.kind, "persistent")
        self.assertTrue(handle.meta["created"])
        self.assertEqual(cli.argv, ["computer", "create", "--name", "cc-crew-a", "--json"])

    def test_ensure_on_an_existing_computer_starts_it_instead_of_failing(self):
        cli = FakeCLI(
            (1, json.dumps({"ok": False, "error": "a computer named cc-crew-a already exists"}), ""),
            envelope({"status": "running"}),
        )
        handle = self.provider(cli).ensure("crew:a", {})
        self.assertFalse(handle.meta["created"])
        self.assertEqual(cli.calls[1][0], ["computer", "start", "cc-crew-a", "--json"])

    def test_ensure_raises_on_a_real_create_failure(self):
        cli = FakeCLI((1, json.dumps({"ok": False, "error": "no hypervisor"}), ""))
        with self.assertRaises(ProviderFailure):
            self.provider(cli).ensure("crew:a", {})

    def test_exec_passes_the_command_after_the_dash_dash(self):
        cli = FakeCLI(envelope({"exit_code": 0, "stdout": "hello\n", "duration_ms": 12}))
        result = self.provider(cli).exec(Handle("celesto-local", "persistent", "cc-x"),
                                        "echo hello", timeout=60)
        argv = cli.argv
        self.assertEqual(argv[:6], ["computer", "exec", "cc-x", "--json", "--start", "--"])
        self.assertEqual(argv[6:], ["sh", "-lc", "echo hello"])
        self.assertEqual(result.stdout, "hello\n")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.duration_ms, 12)

    def test_a_failing_command_is_a_result_not_an_exception(self):
        cli = FakeCLI(envelope({"exit_code": 2, "stdout": "", "stderr": "no such file"}, ok=True,
                               exit_code=2))
        result = self.provider(cli).exec(Handle("celesto-local", "persistent", "cc-x"), "ls /nope")
        self.assertEqual(result.exit_code, 2)
        self.assertFalse(result.ok)
        self.assertEqual(result.stderr, "no such file")

    def test_non_json_output_still_reports_the_process_exit_code(self):
        cli = FakeCLI((3, "plain text", "boom"))
        result = self.provider(cli).exec(Handle("celesto-local", "persistent", "cc-x"), "nope")
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.stdout, "plain text")

    def test_put_file_writes_through_exec_with_base64(self):
        cli = FakeCLI(envelope({"exit_code": 0, "stdout": ""}))
        self.provider(cli).put_file(Handle("celesto-local", "persistent", "cc-x"),
                                    "/workspace/brief.md", "hello 'world'")
        command = cli.argv[8]
        self.assertIn("base64 -d", command)
        self.assertIn("/workspace/brief.md", command)
        encoded = base64.b64encode(b"hello 'world'").decode()
        self.assertIn(encoded, command)

    def test_get_file_decodes_base64(self):
        payload = base64.b64encode(b"notes").decode()
        cli = FakeCLI(envelope({"exit_code": 0, "stdout": payload + "\n"}))
        text = self.provider(cli).get_file(Handle("celesto-local", "persistent", "cc-x"), "/w/a.txt")
        self.assertEqual(text, "notes")

    def test_get_file_failure_raises(self):
        cli = FakeCLI(envelope({"exit_code": 1, "stderr": "missing"}, ok=True, exit_code=1))
        with self.assertRaises(ProviderFailure):
            self.provider(cli).get_file(Handle("celesto-local", "persistent", "cc-x"), "/w/none")

    def test_destroy_tolerates_a_missing_computer(self):
        cli = FakeCLI((1, json.dumps({"ok": False, "error": "computer not found"}), ""))
        self.provider(cli).destroy(Handle("celesto-local", "persistent", "cc-gone"))
        self.assertEqual(cli.argv, ["computer", "delete", "cc-gone", "--json"])

    def test_destroy_raises_on_a_real_failure(self):
        cli = FakeCLI((1, json.dumps({"ok": False, "error": "permission denied"}), ""))
        with self.assertRaises(ProviderFailure):
            self.provider(cli).destroy(Handle("celesto-local", "persistent", "cc-x"))

    def test_is_ready_reads_the_status(self):
        running = FakeCLI(envelope({"status": "running"}))
        self.assertTrue(self.provider(running).is_ready(Handle("celesto-local", "persistent", "cc-x")))
        stopped = FakeCLI(envelope({"status": "stopped"}))
        self.assertFalse(self.provider(stopped).is_ready(Handle("celesto-local", "persistent", "cc-x")))

    def test_list_computers_unwraps_either_shape(self):
        wrapped = FakeCLI(envelope({"computers": [{"name": "a"}, {"name": "b"}]}))
        self.assertEqual([r["name"] for r in self.provider(wrapped).list_computers()], ["a", "b"])
        bare = FakeCLI(envelope([{"name": "c"}]))
        self.assertEqual([r["name"] for r in self.provider(bare).list_computers()], ["c"])

    def test_missing_runtime_refuses_instead_of_using_the_host(self):
        provider = celesto_local.make("celesto-does-not-exist")
        self.assertFalse(provider.configured())
        with self.assertRaises(ProviderNotConfigured) as ctx:
            provider.ensure("crew:a", {})
        self.assertIn("celesto setup", str(ctx.exception.details.get("hint", "")))


if __name__ == "__main__":
    unittest.main()
