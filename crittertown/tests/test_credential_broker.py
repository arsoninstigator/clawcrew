"""brokered credentials: a grant, not a key."""

from __future__ import annotations

import os
import unittest

from base import AppCase

from core.config import Settings
from core.errors import NotFound, ProviderNotConfigured, ValidationError


class VaultCase(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.wren = self.agent(self.team["id"], "wren", role="writer", skills=["writing"])
        # a proxy and a token in this process, the way a deployment would have them
        self.app.vault.settings = Settings(
            database_path=self.settings.database_path,
            vault_proxy_url="http://agent-vault:14322",
            vault_base_url="http://agent-vault:14321",
        )
        self.env = {"WREN_VAULT_TOKEN": "tok_wren_123"}


class VaultTests(VaultCase):
    def test_a_missing_proxy_is_loud_rather_than_silent(self) -> None:
        bare = self.app.vault
        bare.settings = Settings(database_path=self.settings.database_path)
        with self.assertRaises(ProviderNotConfigured):
            bare.grant(self.wren["id"], label="github", token_env="WREN_VAULT_TOKEN")
        self.assertFalse(bare.health()["configured"])
        self.assertIn("CREDENTIAL_BROKER_PROXY_URL", bare.health()["note"])

    def test_a_grant_names_a_variable_and_never_holds_the_secret(self) -> None:
        grant = self.app.vault.grant(self.wren["id"], label="github",
                                     token_env="WREN_VAULT_TOKEN",
                                     scopes=["api.github.com"])
        self.assertEqual(grant["token_env"], "WREN_VAULT_TOKEN")
        self.assertEqual(grant["proxy_url"], "http://agent-vault:14322")
        self.assertEqual(grant["status"], "active")
        self.assertNotIn("tok_wren_123", str(grant))

    def test_token_env_is_required_and_must_be_a_name(self) -> None:
        with self.assertRaises(ValidationError):
            self.app.vault.grant(self.wren["id"], label="x", token_env="")
        with self.assertRaises(ValidationError):
            self.app.vault.grant(self.wren["id"], label="x", token_env="not a name")

    def test_the_sandbox_gets_a_proxy_and_a_token(self) -> None:
        self.app.vault.grant(self.wren["id"], label="github", token_env="WREN_VAULT_TOKEN")
        env = self.app.vault.sandbox_env(self.wren["id"], env=self.env)
        self.assertEqual(env["HTTPS_PROXY"], "http://agent-vault:14322")
        self.assertEqual(env["HTTP_PROXY"], "http://agent-vault:14322")
        self.assertEqual(env["WREN_VAULT_TOKEN"], "tok_wren_123")

    def test_a_missing_token_variable_is_loud(self) -> None:
        self.app.vault.grant(self.wren["id"], label="github", token_env="WREN_VAULT_TOKEN")
        with self.assertRaises(ProviderNotConfigured):
            self.app.vault.sandbox_env(self.wren["id"], env={})

    def test_the_described_env_masks_the_value(self) -> None:
        self.app.vault.grant(self.wren["id"], label="github", token_env="WREN_VAULT_TOKEN")
        described = self.app.vault.describe_env(self.wren["id"], env=self.env)
        self.assertEqual(described["env"]["WREN_VAULT_TOKEN"], "<set>")
        self.assertNotIn("tok_wren_123", str(described))
        missing = self.app.vault.describe_env(self.wren["id"], env={})
        self.assertEqual(missing["env"]["WREN_VAULT_TOKEN"], "<missing>")

    def test_no_agent_gets_the_whole_internet(self) -> None:
        self.assertFalse(self.app.vault.egress_allowed(self.wren["id"], "api.github.com")["allowed"])
        self.app.vault.grant(self.wren["id"], label="github", token_env="WREN_VAULT_TOKEN",
                             scopes=["github.com"])
        self.assertTrue(self.app.vault.egress_allowed(self.wren["id"], "api.github.com")["allowed"])
        self.assertTrue(self.app.vault.egress_allowed(self.wren["id"], "github.com")["allowed"])
        verdict = self.app.vault.egress_allowed(self.wren["id"], "evil.example.com")
        self.assertFalse(verdict["allowed"])
        self.assertIn("scopes", verdict)

    def test_an_unscoped_grant_defers_to_the_proxy(self) -> None:
        self.app.vault.grant(self.wren["id"], label="all", token_env="WREN_VAULT_TOKEN")
        verdict = self.app.vault.egress_allowed(self.wren["id"], "anything.example")
        self.assertTrue(verdict["allowed"])
        self.assertIn("proxy", verdict["reason"])

    def test_revoking_stops_the_next_spawn_and_keeps_the_record(self) -> None:
        grant = self.app.vault.grant(self.wren["id"], label="github", token_env="WREN_VAULT_TOKEN")
        revoked = self.app.vault.revoke(grant["id"], actor="owner", reason="offboarded")
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(self.app.vault.sandbox_env(self.wren["id"], env=self.env), {})
        self.assertEqual(len(self.app.vault.list(agent_id=self.wren["id"])), 1)
        with self.assertRaises(NotFound):
            self.app.vault.revoke("vlt_nope")

    def test_a_grant_in_the_past_is_expired_on_read(self) -> None:
        self.app.vault.grant(self.wren["id"], label="short", token_env="WREN_VAULT_TOKEN",
                             expires_hours=-1)
        self.assertEqual(self.app.vault.active(self.wren["id"]), [])
        self.assertEqual(self.app.vault.list(agent_id=self.wren["id"])[0]["status"], "expired")

    def test_regranting_the_same_label_replaces_it(self) -> None:
        first = self.app.vault.grant(self.wren["id"], label="github", token_env="WREN_VAULT_TOKEN",
                                    scopes=["github.com"])
        self.app.vault.revoke(first["id"])
        second = self.app.vault.grant(self.wren["id"], label="github", token_env="WREN_VAULT_TOKEN",
                                      scopes=["api.github.com", "github.com"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["status"], "active")
        self.assertEqual(len(self.app.vault.list(agent_id=self.wren["id"])), 1)

    def test_every_change_lands_in_the_audit_chain(self) -> None:
        grant = self.app.vault.grant(self.wren["id"], label="github", token_env="WREN_VAULT_TOKEN")
        self.app.vault.revoke(grant["id"])
        rows = self.app.store.query(
            "select action from agent_audit where agent_id = ? order by seq",
            (self.wren["id"],),
        )
        self.assertEqual([r["action"] for r in rows], ["vault.granted", "vault.revoked"])

    def test_the_api_never_returns_a_secret(self) -> None:
        from fastapi.testclient import TestClient
        from api.routes import create_api

        self.app.vault.env = self.env
        client = TestClient(create_api(self.app))
        created = client.post(f"/agents/{self.wren['id']}/vault",
                              json={"label": "github", "token_env": "WREN_VAULT_TOKEN"}).json()
        self.assertEqual(created["status"], "active")
        listed = client.get(f"/agents/{self.wren['id']}/vault").json()
        self.assertNotIn("tok_wren_123", str(listed))
        self.assertEqual(listed["env"]["env"]["WREN_VAULT_TOKEN"], "<set>")
        health = client.get("/vault").json()
        self.assertTrue(health["configured"])
        allowed = client.post("/vault/egress",
                              json={"agent_id": self.wren["id"], "host": "api.github.com"}).json()
        self.assertTrue(allowed["allowed"])
        revoked = client.request("DELETE", f"/vault/{created['id']}",
                                 params={"reason": "done"}).json()
        self.assertEqual(revoked["status"], "revoked")


if __name__ == "__main__":
    unittest.main()
