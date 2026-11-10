"""superserve: the sdk is the contract, so the adapter never guesses an endpoint."""

from __future__ import annotations

import unittest

from clawcrew.create.sandbox import superserve
from clawcrew.errors import ProviderFailure, ProviderNotConfigured


class FakeResult:
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr


class FakeFiles:
    def __init__(self):
        self.written = {}

    def write(self, path, payload):
        self.written[path] = payload
        return {"path": path}

    def read_text(self, path):
        return self.written[path].decode()


class FakeSandbox:
    instances: list["FakeSandbox"] = []
    fail_on_create = False

    def __init__(self, name, template=""):
        self.id = f"sb_{name}"
        self.name = name
        self.template = template
        self.files = FakeFiles()
        self.commands = self
        self.killed = False
        self.paused = False
        self.ran: list[tuple[str, int]] = []

    @classmethod
    def create(cls, **kwargs):
        if cls.fail_on_create:
            raise RuntimeError("quota")
        box = cls(kwargs.get("name", ""), kwargs.get("from_template", ""))
        box.kwargs = kwargs
        cls.instances.append(box)
        return box

    def run(self, command, timeout_seconds=300):
        self.ran.append((command, timeout_seconds))
        return FakeResult(0, f"ran {command}")

    def kill(self):
        self.killed = True

    def pause(self):
        self.paused = True

    def get_signed_preview_url(self, port, expires_in_seconds=300):
        return f"https://preview.test/{self.id}/{port}?exp={expires_in_seconds}"


class FakeModule:
    Sandbox = FakeSandbox


class NoConnectSandbox(FakeSandbox):
    connect = None


class NoConnectModule:
    Sandbox = NoConnectSandbox


class SuperserveProviderTests(unittest.TestCase):
    def setUp(self):
        FakeSandbox.instances = []
        FakeSandbox.fail_on_create = False
        FakeSandbox.connect = None

    def provider(self, key="ss_live_x", module=None):
        return superserve.make(key, "https://api.superserve.ai", "", module=module or FakeModule)

    def test_an_unconfigured_provider_says_exactly_what_is_missing(self):
        self.assertIn("SUPERSERVE_API_KEY", superserve.make("").why_not())
        self.assertIn("pip install superserve", superserve.make("k", module=None).why_not()
                      if superserve.sdk() is None else "")
        with self.assertRaises(ProviderNotConfigured):
            self.provider(key="").ensure("agent-1", {})

    def test_create_translates_the_spec_and_returns_a_handle(self):
        handle = self.provider().ensure("agent-1", {"name": "box", "template": "superserve/desktop"})
        self.assertEqual((handle.provider, handle.ref), ("superserve", "sb_box"))
        self.assertEqual(handle.meta["template"], "superserve/desktop")
        self.assertEqual(FakeSandbox.instances[0].kwargs["api_key"], "ss_live_x")

    def test_exec_and_files_go_through_the_sdk_surface(self):
        provider = self.provider()
        handle = provider.ensure("agent-1", {"name": "box"})
        result = provider.exec(handle, "echo hi", timeout=42)
        self.assertEqual((result.exit_code, result.stdout), (0, "ran echo hi"))
        self.assertEqual(FakeSandbox.instances[0].ran, [("echo hi", 42)])
        provider.put_file(handle, "/work/a.txt", "hello")
        self.assertEqual(provider.get_file(handle, "/work/a.txt"), "hello")

    def test_a_preview_link_is_how_a_human_looks_at_the_result(self):
        provider = self.provider()
        handle = provider.ensure("agent-1", {"name": "box"})
        link = provider.preview_url(handle, 3000, expires_in=60)
        self.assertEqual(link["url"], "https://preview.test/sb_box/3000?exp=60")

    def test_destroy_kills_it(self):
        provider = self.provider()
        handle = provider.ensure("agent-1", {"name": "box"})
        provider.destroy(handle)
        self.assertTrue(FakeSandbox.instances[0].killed)

    def test_a_stale_handle_is_reported_instead_of_pretended(self):
        provider = self.provider(module=NoConnectModule)
        handle = provider.ensure("agent-1", {"name": "box"})
        provider._live.clear()
        with self.assertRaises(ProviderFailure):
            provider.exec(handle, "ls")

    def test_a_sdk_with_connect_can_reattach(self):
        FakeSandbox.connect = classmethod(
            lambda cls, ref, **kwargs: FakeSandbox(ref, "")
        )
        provider = self.provider()
        handle = provider.ensure("agent-1", {"name": "box"})
        provider._live.clear()
        self.assertEqual(provider.exec(handle, "ls").stdout, "ran ls")

    def test_a_vendor_failure_keeps_its_message(self):
        FakeSandbox.fail_on_create = True
        with self.assertRaises(ProviderFailure) as caught:
            self.provider().ensure("agent-1", {"name": "box"})
        self.assertIn("quota", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
