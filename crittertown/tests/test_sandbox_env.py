"""env reaching a sandbox, and the two ways it must not.

the broker's `sandbox_env()` was tested and unused: nothing handed the result to a
provider, so a proxied credential could never reach a sandbox. these tests hold the
wiring that now does, and the rule that makes it safe: a provider that can take env in
its create request gets the values and nothing records them, and a provider that cannot
(a computer, whose create schema has no env field) gets nothing at create time and its
harness pulls the same values at boot instead.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from base import AppCase

from core.config import Settings
from core.errors import NotFound, ValidationError
from sandbox.base import clean_env
from sandbox.local import LocalProvider

SECRET = "tok_wren_123"


class CleanEnvTests(unittest.TestCase):
    def test_a_plain_env_is_returned(self):
        self.assertEqual(clean_env({"A": "1", "B_2": "two"}), {"A": "1", "B_2": "two"})

    def test_an_empty_env_is_empty(self):
        self.assertEqual(clean_env(None), {})
        self.assertEqual(clean_env({}), {})

    def test_a_newline_is_refused(self):
        with self.assertRaises(ValidationError):
            clean_env({"A": "one\ntwo"})

    def test_a_nul_is_refused(self):
        with self.assertRaises(ValidationError):
            clean_env({"A": "one\0two"})

    def test_a_name_that_is_not_a_name_is_refused(self):
        for bad in ("A B", "A=B", "", "1A", "A-B"):
            with self.assertRaises(ValidationError):
                clean_env({bad: "x"})

    def test_a_value_that_is_not_a_string_is_refused(self):
        with self.assertRaises(ValidationError):
            clean_env({"A": 1})


class LocalProviderEnvTests(unittest.TestCase):
    """the dev provider runs real subprocesses, so it can prove the env actually lands."""

    def test_a_command_sees_the_env_it_was_handed(self):
        provider = LocalProvider(tempfile.mkdtemp(prefix="envtest-"))
        handle = provider.ensure("envtest", {}, {"SANDBOX_TOKEN": SECRET})
        result = provider.exec(handle, 'python3 -c "import os;print(os.environ.get(\'SANDBOX_TOKEN\',\'\'))"')
        self.assertEqual(result.stdout.strip(), SECRET)

    def test_a_second_handle_does_not_leak_the_first_env(self):
        provider = LocalProvider(tempfile.mkdtemp(prefix="envtest-"))
        first = provider.ensure("one", {}, {"SANDBOX_TOKEN": SECRET})
        second = provider.ensure("two", {})
        self.assertEqual(provider.env_for(first), {"SANDBOX_TOKEN": SECRET})
        self.assertEqual(provider.env_for(second), {})

    def test_a_bad_value_is_refused_before_the_handle_exists(self):
        provider = LocalProvider(tempfile.mkdtemp(prefix="envtest-"))
        with self.assertRaises(ValidationError):
            provider.ensure("bad", {}, {"SANDBOX_TOKEN": "line\nbreak"})


class EphemeralSpecTests(unittest.TestCase):
    def provider(self):
        from ingredients.machines.ephemeral.client import EphemeralProvider

        return EphemeralProvider(api_key="k", template="base")

    def test_the_create_body_carries_the_env(self):
        body = self.provider().build_spec("k", {}, {"SANDBOX_TOKEN": SECRET})
        self.assertEqual(body["env_vars"], {"SANDBOX_TOKEN": SECRET})

    def test_the_argument_wins_over_the_spec(self):
        body = self.provider().build_spec(
            "k", {"env": {"SANDBOX_TOKEN": "stale", "KEPT": "1"}}, {"SANDBOX_TOKEN": SECRET}
        )
        self.assertEqual(body["env_vars"]["SANDBOX_TOKEN"], SECRET)
        self.assertEqual(body["env_vars"]["KEPT"], "1")

    def test_no_env_means_no_key(self):
        self.assertNotIn("env_vars", self.provider().build_spec("k", {}))


class ProviderFlagTests(unittest.TestCase):
    def test_the_providers_that_cannot_take_env_say_so(self):
        from ingredients.machines.hosted.client import HostedProvider
        from ingredients.machines.persistent.client import PersistentProvider

        self.assertFalse(PersistentProvider.delivers_env)
        self.assertFalse(HostedProvider.delivers_env)

    def test_the_providers_that_can_say_so(self):
        from ingredients.machines.ephemeral.client import EphemeralProvider
        from sandbox.local import LocalProvider

        self.assertTrue(EphemeralProvider.delivers_env)
        self.assertTrue(LocalProvider.delivers_env)

    def test_every_provider_accepts_the_argument(self):
        from ingredients.machines.hosted.client import HostedProvider
        from ingredients.machines.persistent.client import PersistentProvider
        from ingredients.machines.ephemeral.client import EphemeralProvider

        for provider in (HostedProvider, PersistentProvider, EphemeralProvider, LocalProvider):
            signature = provider.ensure.__code__.co_varnames[:provider.ensure.__code__.co_argcount]
            self.assertIn("env", signature, f"{provider.__name__}.ensure does not take env")


class CrewHandsEnvTests(AppCase):
    """the wiring: a grant becomes an env, and the env reaches only the provider."""

    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.wren = self.agent(self.team["id"], "wren", role="writer", skills=["writing"])
        self.app.vault.settings = Settings(
            database_path=self.settings.database_path,
            vault_proxy_url="http://agent-vault:14322",
            vault_base_url="http://agent-vault:14321",
        )
        self.app.vault.env = {"WREN_VAULT_TOKEN": SECRET}
        self.app.vault.grant(self.wren["id"], label="github", token_env="WREN_VAULT_TOKEN")

    def test_a_provider_that_takes_env_is_handed_it(self):
        seen = {}

        class Provider:
            name = "fake"
            delivers_env = True

            def ensure(self, key, spec, env=None):
                seen["env"] = dict(env or {})
                from sandbox.base import Handle

                return Handle(provider="fake", kind="persistent", ref="ref-1")

        self.crews.registry = mock.Mock()
        self.crews.registry.get.return_value = Provider()
        self.crews.ensure_computer(self.wren["id"])
        self.assertEqual(seen["env"]["WREN_VAULT_TOKEN"], SECRET)
        self.assertEqual(seen["env"]["HTTPS_PROXY"], "http://agent-vault:14322")

    def test_a_provider_that_cannot_is_handed_nothing(self):
        seen = {}

        class Provider:
            name = "fake"
            delivers_env = False

            def ensure(self, key, spec, env=None):
                seen["env"] = dict(env or {})
                from sandbox.base import Handle

                return Handle(provider="fake", kind="persistent", ref="ref-2")

        self.crews.registry = mock.Mock()
        self.crews.registry.get.return_value = Provider()
        self.crews.ensure_computer(self.wren["id"])
        self.assertEqual(seen["env"], {})

    def test_the_secret_never_reaches_the_row_or_the_response(self):
        class Provider:
            name = "fake"
            delivers_env = True

            def ensure(self, key, spec, env=None):
                from sandbox.base import Handle

                return Handle(provider="fake", kind="persistent", ref="ref-3")

        self.crews.registry = mock.Mock()
        self.crews.registry.get.return_value = Provider()
        response = self.crews.ensure_computer(self.wren["id"])
        row = self.app.store.query("select * from computers where agent_id = ?", (self.wren["id"],))
        self.assertNotIn(SECRET, json.dumps(response, default=str))
        self.assertNotIn(SECRET, json.dumps([dict(r) for r in row], default=str))
        self.assertNotIn("WREN_VAULT_TOKEN", json.dumps(response, default=str))

    def test_a_provider_error_does_not_leak_the_env_into_the_message(self):
        class Provider:
            name = "fake"
            delivers_env = True

            def ensure(self, key, spec, env=None):
                raise RuntimeError(f"create failed for {sorted((env or {}))}")

        self.crews.registry = mock.Mock()
        self.crews.registry.get.return_value = Provider()
        with self.assertRaises(RuntimeError) as caught:
            self.crews.ensure_computer(self.wren["id"])
        # the names may appear; the value must not, even in a provider's own error text
        self.assertNotIn(SECRET, str(caught.exception))


class SandboxEnvRouteTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.wren = self.agent(self.team["id"], "wren", role="writer", skills=["writing"])
        self.other = self.agent(self.team["id"], "bo", role="builder", skills=["coding"])
        self.app.vault.settings = Settings(
            database_path=self.settings.database_path,
            vault_proxy_url="http://agent-vault:14322",
            vault_base_url="http://agent-vault:14321",
        )
        self.app.vault.env = {"WREN_VAULT_TOKEN": SECRET}
        self.app.vault.grant(self.wren["id"], label="github", token_env="WREN_VAULT_TOKEN")
        from fastapi.testclient import TestClient

        from api.routes import create_api

        self.client = TestClient(create_api(self.app))

    def test_the_harness_pulls_its_own_env(self):
        body = self.client.get(f"/agents/{self.wren['id']}/sandbox-env").json()
        self.assertEqual(body["env"]["WREN_VAULT_TOKEN"], SECRET)
        self.assertTrue(body["env"]["CREDENTIAL_BROKER_GRANT"].startswith("vlt_"))

    def test_an_agent_with_no_grant_gets_an_empty_env(self):
        body = self.client.get(f"/agents/{self.other['id']}/sandbox-env").json()
        self.assertEqual(body["env"], {})

    def test_an_unknown_agent_is_a_404(self):
        self.assertEqual(self.client.get("/agents/agt_nope/sandbox-env").status_code, 404)

    def test_the_describing_route_still_masks(self):
        body = self.client.get(f"/agents/{self.wren['id']}/vault").json()
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertEqual(body["env"]["env"]["WREN_VAULT_TOKEN"], "<set>")


class HarnessPullTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_the_pull_applies_values_and_returns_only_names(self):
        from agents import runtime

        calls = {}

        def fake_request_json(method, url, payload=None, headers=None, timeout=60):
            calls["url"] = url
            calls["headers"] = headers
            return {"agent_id": "agt_1", "env": {"SANDBOX_TOKEN": SECRET}}

        with mock.patch.object(runtime, "request_json", fake_request_json):
            result = runtime.pull_env("http://platform:8000", "agt_1", token="t")

        self.assertEqual(os.environ["SANDBOX_TOKEN"], SECRET)
        self.assertEqual(result["names"], ["SANDBOX_TOKEN"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(calls["url"], "http://platform:8000/agents/agt_1/sandbox-env")
        self.assertEqual(calls["headers"]["authorization"], "Bearer t")

    def test_a_platform_that_is_unreachable_does_not_stop_a_boot(self):
        from agents import runtime

        with mock.patch.object(runtime, "request_json", side_effect=OSError("down")):
            result = runtime.pull_env("http://platform:8000", "agt_1")
        self.assertEqual(result["loaded"], 0)
        self.assertIn("error", result)

    def test_no_platform_url_is_not_an_error(self):
        from agents import runtime

        self.assertEqual(runtime.pull_env("", "agt_1")["loaded"], 0)


if __name__ == "__main__":
    unittest.main()
