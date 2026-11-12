"""agentids, signed attestations, subagent ids, and the pairing with comms.

the promises under test:
  an agentid is derived from the agent's key, so nothing without the key can claim it
  every audit row is signed, and editing one breaks verification instead of passing
  a bundle verifies with no database and no process: it is the row, not our word
  a subagent id is provable from its parent's signed spawn, and a fan-in names lanes
  the same key signs room events and audit rows, and that agreement is checkable
"""

from __future__ import annotations

import unittest

import base  # noqa: F401
from base import AppCase

from manage import attest
from core.errors import NotAllowed, ValidationError


class AgentIdTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.agent = self.agent(self.team["id"], "wren")

    def test_agentid_is_derived_from_the_agents_own_key(self):
        identity = self.app.comms.identity(self.agent["id"])
        self.assertEqual(self.agent["agentid"], identity["agentid"])
        self.assertTrue(attest.agentid_matches(self.agent["agentid"], identity["pubkey"]))
        self.assertTrue(self.agent["agentid"].startswith("agent1"))

    def test_agentid_cannot_be_claimed_without_the_key(self):
        other = attest.keys.generate()
        self.assertFalse(attest.agentid_matches(self.agent["agentid"], other.pubkey))
        self.assertFalse(attest.is_agentid("agent1notreally"))
        self.assertFalse(attest.is_agentid(self.agent["id"]))

    def test_lookup_by_agentid_and_stable_across_rename(self):
        found = self.crews.by_agentid(self.agent["agentid"])
        self.assertEqual(found["id"], self.agent["id"])
        renamed = self.crews.update_agent(self.agent["id"], name="wren-two")
        self.assertEqual(renamed["agentid"], self.agent["agentid"])

    def test_agentid_is_set_on_creation_not_later(self):
        fresh = self.crews.add_agent(self.team["id"], "okafor")
        self.assertTrue(attest.is_agentid(fresh["agentid"]))


class SignedTrailTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.agent = self.agent(self.team["id"], "wren")

    def _entries(self, agent_id: str):
        return self.audit_entries(agent_id)

    def audit_entries(self, agent_id: str):
        return self.app.audit.history(agent_id, limit=100)

    def test_every_row_is_signed_by_the_agents_key(self):
        for _ in range(3):
            self.app.audit.record(team_id=self.team["id"], agent_id=self.agent["id"],
                                  action="agent.created")
        history = self._entries(self.agent["id"])
        self.assertEqual(len(history), 3)
        for entry in history:
            self.assertTrue(entry["attested"])
            self.assertEqual(entry["signer"], self.agent["agentid"])
            self.assertTrue(attest.verify_signature(entry["sig"], entry["hash"], entry["pubkey"]))
        verified = self.app.audit.verify(self.agent["id"])
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["signed"], 3)
        self.assertEqual(verified["unsigned"], 0)

    def test_editing_a_row_breaks_the_chain(self):
        for _ in range(3):
            self.app.audit.record(team_id=self.team["id"], agent_id=self.agent["id"],
                                  action="agent.created")
        with self.store_tx() as c:
            c.execute("update agent_audit set payload = '{\"tampered\":true}' where agent_id = ? and seq = 2",
                      (self.agent["id"],))
        broken = self.app.audit.verify(self.agent["id"])
        self.assertFalse(broken["ok"])
        self.assertEqual(broken["broken_at_seq"], 2)

    def test_a_forged_signature_is_caught_even_when_the_chain_is_intact(self):
        self.app.audit.record(team_id=self.team["id"], agent_id=self.agent["id"],
                              action="agent.created")
        other = attest.keys.generate()
        with self.store_tx() as c:
            c.execute("update agent_audit set sig = ? where agent_id = ?",
                      (other.sign(bytes.fromhex(self._entries(self.agent["id"])[0]["hash"])),
                       self.agent["id"]))
        broken = self.app.audit.verify(self.agent["id"])
        self.assertFalse(broken["ok"])
        self.assertEqual(broken["bad_signature_at_seq"], 1)
        self.assertIsNone(broken["broken_at_seq"])

    def store_tx(self):
        return self.app.store.tx()

    def test_bundle_verifies_without_the_database(self):
        self.app.audit.record(team_id=self.team["id"], agent_id=self.agent["id"],
                              action="agent.created",
                              target_kind="task", target_id="task_x",
                              payload={"note": "signed at the moment of the act"})
        bundle = self.app.audit.export(self.agent["id"])
        self.assertEqual(bundle["kind"], "crittertown.attestation")
        self.assertTrue(bundle["verification"]["ok"])
        # verify the entries alone: no store, no service, exactly what a recipient has
        checked = attest.verify_entries(bundle["entries"])
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["signed"], 1)
        self.assertEqual(checked["head"], bundle["verification"]["head"])

        tampered = [dict(entry) for entry in bundle["entries"]]
        tampered[0]["payload"] = {"note": "someone edited history"}
        self.assertFalse(attest.verify_entries(tampered)["ok"])

    def test_a_node_without_custody_reports_unsigned_rather_than_faking(self):
        from manage.audit import AuditService
        from core.db import Store

        store = Store(self.settings.database_path)
        # an agent this node has no key for: keys held elsewhere is a real deployment,
        # and the trail has to say "unsigned" rather than pretend.
        with store.tx() as c:
            c.execute(
                """insert into agents (id, team_id, name, status, agent_key, created_date,
                       updated_date) values ('agent_blind', ?, 'mute', 'idle', 'ACBLIND0001', 0, 0)""",
                (self.team["id"],),
            )
        blind = AuditService(store, keys=attest.KeyStore(store, mint=False))
        entry = blind.record(team_id=self.team["id"], agent_id="agent_blind",
                             action="agent.created")
        self.assertFalse(entry["attested"])
        verified = blind.verify("agent_blind")
        self.assertTrue(verified["ok"])
        self.assertEqual(verified["unsigned"], 1)
        self.assertEqual(verified["signed"], 0)


class PairingTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.agent = self.agent(self.team["id"], "wren")

    def test_the_same_key_signs_audit_rows_and_room_events(self):
        channel = self.app.channels.create(self.team["id"], "general")
        self.app.comms.identity(self.agent["id"])
        self.app.channels.post(
            channel["id"],
            "a message that should be signed by the same principal",
            author_id=self.agent["id"],
            author_kind="agent",
        )
        principal = self.app.audit.paired(self.agent["id"])
        self.assertTrue(principal["same_key"])
        self.assertTrue(principal["agree"])
        self.assertEqual(principal["agentid"], self.agent["agentid"])
        self.assertEqual(principal["comms"]["agentid"], principal["audit"]["signer"] and principal["agentid"])
        self.assertGreaterEqual(principal["comms_events_signed"], 1)

    def test_the_principal_view_names_the_agent_and_its_key(self):
        principal = self.app.audit.paired(self.agent["id"])
        self.assertEqual(principal["comms"]["pubkey"], self.app.comms.keypair(self.agent["id"]).pubkey)
        self.assertTrue(principal["comms"]["npub"].startswith("npub1"))


class SubagentIdTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.lead = self.agent(self.team["id"], "lead", role="lead")
        self.app.profile.set_spawn_policy(self.lead["id"], enabled=True, max_children=4,
                                          max_depth=2, mode="ephemeral")

    def test_a_subagent_id_is_provable_from_its_parent(self):
        spawn = self.app.profile.spawn(self.lead["id"], "lane-a", task_id="task_1", index=0)
        self.assertTrue(attest.is_subagent_id(spawn["subagent_id"]))
        row = self.app.store.one(
            "select * from agent_spawns where id = ?", (spawn["spawn_id"],)
        )
        self.assertTrue(
            attest.subagent_matches(
                spawn["subagent_id"], spawn["parent_agentid"], "task_1", 0, row["nonce"]
            )
        )
        # a different lane, a different id
        second = self.app.profile.spawn(self.lead["id"], "lane-b", task_id="task_1", index=1)
        self.assertNotEqual(spawn["subagent_id"], second["subagent_id"])

    def test_a_subagent_id_cannot_be_forged_by_another_parent(self):
        spawn = self.app.profile.spawn(self.lead["id"], "lane-a", task_id="task_1", index=0)
        other = self.crews.add_agent(self.team["id"], "outsider")
        forged = attest.subagent_id(other["agentid"], "task_1", 0, "nonce")
        self.assertNotEqual(forged, spawn["subagent_id"])

    def test_spawn_is_attested_by_the_parent_and_names_the_child(self):
        spawn = self.app.profile.spawn(self.lead["id"], "lane-a", task_id="task_1", index=0)
        entry = self.app.audit.history(self.lead["id"], action="subagent.spawned")[0]
        self.assertTrue(entry["attested"])
        self.assertEqual(entry["target_id"], spawn["subagent_id"])
        self.assertEqual(entry["payload"]["child_agent_id"], spawn["child"]["id"])
        self.assertTrue(attest.is_agentid(entry["payload"]["child_agentid"]))

    def test_fan_out_then_fan_in_names_the_lanes_that_reported(self):
        out = self.app.profile.spawn_fan_out(
            self.lead["id"], ["lane-a", "lane-b", "lane-c"], task_id="task_9"
        )
        self.assertEqual(len(out["lanes"]), 3)
        self.assertEqual([lane["fan_out"] for lane in out["lanes"]], [0, 1, 2])
        ids = out["subagent_ids"]
        merged = self.app.profile.fan_in(
            self.lead["id"], "task_9", {ids[0]: {"found": 2}, ids[2]: {"found": 0}}
        )
        self.assertEqual(merged["lane_count"], 3)
        self.assertEqual(sorted(merged["merged"]), sorted([ids[0], ids[2]]))
        self.assertEqual(merged["missing"], [ids[1]])
        entry = self.app.audit.history(self.lead["id"], action="subagent.fan_in")[0]
        self.assertTrue(entry["attested"])
        self.assertEqual(entry["target_id"], "task_9")

    def test_a_lane_that_never_reported_is_reported_not_hidden(self):
        out = self.app.profile.spawn_fan_out(self.lead["id"], ["a", "b"], task_id="task_2")
        merged = self.app.profile.fan_in(self.lead["id"], "task_2", {})
        self.assertEqual(merged["lane_count"], 2)
        self.assertEqual(len(merged["missing"]), 2)
        self.assertEqual(merged["merged"], [])


class WorkflowAttestationTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.agent = self.agent(self.team["id"], "wren")

    def test_a_workflow_has_a_signed_trail_naming_its_signers(self):
        workflow = self.app.workflows.save(
            self.team["id"], "nightly-digest",
            {"nodes": [{"id": "digest", "type": "set_context",
                        "values": {"last": "ran"}}]},
        )
        self.app.audit.record(team_id=self.team["id"], agent_id=self.agent["id"],
                              action="workflow.proposed", target_kind="workflow",
                              target_id=workflow["id"], payload={"name": "nightly-digest"})
        bundle = self.app.audit.workflow_attestation(workflow["id"])
        self.assertEqual(bundle["kind"], "crittertown.workflow-attestation")
        self.assertEqual(bundle["signers"], [self.agent["agentid"]])
        self.assertTrue(bundle["verification"]["ok"])
        self.assertEqual(bundle["verification"]["signed"], 1)


if __name__ == "__main__":
    unittest.main()
