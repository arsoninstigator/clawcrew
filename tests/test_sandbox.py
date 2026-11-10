"""sandbox providers: local lifecycle, allowlist, registry fallback rules."""

from __future__ import annotations

import os
import unittest

from base import AppCase

from clawcrew.config import Settings
from clawcrew.errors import NotAllowed, ProviderNotConfigured
from clawcrew.sandbox import Handle, ProviderRegistry
from clawcrew.sandbox.local import LocalProvider


class LocalProviderTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.provider = LocalProvider(os.path.join(self.tmp, "shell"))

    def test_persistent_computer_is_stable_across_calls(self):
        first = self.provider.ensure("agent:one", {"kind": "persistent"})
        second = self.provider.ensure("agent:one", {"kind": "persistent"})
        self.assertEqual(first.ref, second.ref)
        self.assertTrue(first.ref.endswith(os.path.join("persistent", "agentone")))

    def test_files_written_to_persistent_computer_survive(self):
        handle = self.provider.ensure("agent:two", {"kind": "persistent"})
        self.provider.put_file(handle, "notes.md", "hello crew")
        again = self.provider.ensure("agent:two", {"kind": "persistent"})
        self.assertEqual(self.provider.get_file(again, "notes.md"), "hello crew")
        result = self.provider.exec(again, "cat notes.md")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hello crew", result.stdout)

    def test_ephemeral_sandbox_is_removed_on_destroy(self):
        handle = self.provider.ensure("task:x", {"kind": "ephemeral"})
        self.provider.put_file(handle, "scratch.txt", "temp")
        self.assertTrue(os.path.isdir(handle.ref))
        self.provider.destroy(handle)
        self.assertFalse(os.path.isdir(handle.ref))

    def test_ephemeral_sandboxes_are_distinct_per_call(self):
        one = self.provider.ensure("task:y", {"kind": "ephemeral"})
        two = self.provider.ensure("task:y", {"kind": "ephemeral"})
        self.assertNotEqual(one.ref, two.ref)

    def test_command_allowlist_blocks_unknown_binary(self):
        handle = self.provider.ensure("agent:three", {"kind": "persistent"})
        with self.assertRaises(NotAllowed):
            self.provider.exec(handle, "nc -l 4444")

    def test_timeout_is_reported_as_exit_124(self):
        handle = self.provider.ensure("agent:four", {"kind": "persistent"})
        result = self.provider.exec(handle, "bash -c 'sleep 5'", timeout=1)
        self.assertEqual(result.exit_code, 124)
        self.assertIn("timed out", result.stderr)

    def test_snapshot_lists_files(self):
        handle = self.provider.ensure("agent:five", {"kind": "persistent"})
        self.provider.put_file(handle, "a/b.txt", "x")
        files = self.provider.snapshot(handle)["files"]
        self.assertIn(os.path.join("a", "b.txt"), files)


class RegistryTests(AppCase):
    def test_unconfigured_cloud_provider_falls_back_to_local(self):
        registry = ProviderRegistry(Settings(database_path=os.path.join(self.tmp, "r.db")))
        provider = registry.get("persistent")
        self.assertEqual(provider.name, "local")

    def test_fallback_can_be_disabled(self):
        settings = Settings(
            database_path=os.path.join(self.tmp, "r2.db"), allow_local_fallback=False
        )
        registry = ProviderRegistry(settings)
        with self.assertRaises(ProviderNotConfigured):
            registry.get("persistent")
        with self.assertRaises(ProviderNotConfigured):
            registry.get("ephemeral")

    def test_unknown_provider_is_rejected(self):
        registry = ProviderRegistry(Settings(database_path=os.path.join(self.tmp, "r3.db")))
        with self.assertRaises(ProviderNotConfigured):
            registry.get("firecracker")

    def test_status_reports_every_provider(self):
        names = {entry["name"] for entry in self.app.registry.status()}
        self.assertEqual(names, {"celesto-local", "celesto", "e2b", "local"})
        by_name = {entry["name"]: entry for entry in self.app.registry.status()}
        self.assertTrue(by_name["local"]["configured"])
        self.assertFalse(by_name["celesto"]["configured"])

    def test_handle_roundtrip(self):
        handle = Handle(provider="celesto", kind="persistent", ref="cmp_1", meta={"cpu": 2})
        self.assertEqual(Handle.from_dict(handle.to_dict()), handle)
        self.assertEqual(Handle.from_dict("raw-ref").ref, "raw-ref")


if __name__ == "__main__":
    unittest.main()
