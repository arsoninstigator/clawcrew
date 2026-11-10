"""coordination: one owner per key, and a stream that survives the writer."""

from __future__ import annotations

import unittest

from base import AppCase

from clawcrew.config import Settings
from clawcrew.create.coordination import SqliteCoordination, actor_contract, plan_runtime
from clawcrew.create.coordination.durable_actors import CHART, SERVICE
from clawcrew.errors import ValidationError


class LeaseTests(AppCase):
    def setUp(self):
        super().setUp()
        self.store = self.app.store
        self.coordination = SqliteCoordination(self.store)

    def test_only_one_holder_at_a_time(self):
        first = self.coordination.claim("crew:c1", "card:1", "agent:a")
        second = self.coordination.claim("crew:c1", "card:1", "agent:b")
        self.assertTrue(first["granted"])
        self.assertFalse(second["granted"])
        self.assertEqual(second["holder"], "agent:a")

    def test_the_same_holder_can_renew(self):
        self.coordination.claim("crew:c1", "card:1", "agent:a", ttl_seconds=60)
        again = self.coordination.claim("crew:c1", "card:1", "agent:a", ttl_seconds=60)
        self.assertTrue(again["granted"])

    def test_a_crashed_holder_cannot_hold_the_board_forever(self):
        self.coordination.claim("crew:c1", "card:1", "agent:a", ttl_seconds=1)
        with self.store.tx() as conn:
            conn.execute("update coordination_leases set expires_at = 0")
        taken = self.coordination.claim("crew:c1", "card:1", "agent:b")
        self.assertTrue(taken["granted"])
        self.assertEqual(taken["holder"], "agent:b")

    def test_only_the_holder_can_release(self):
        self.coordination.claim("crew:c1", "card:1", "agent:a")
        self.assertFalse(self.coordination.release("crew:c1", "card:1", "agent:b")["released"])
        self.assertTrue(self.coordination.release("crew:c1", "card:1", "agent:a")["released"])
        self.assertEqual(self.coordination.leases("crew:c1"), [])

    def test_different_keys_do_not_block_each_other(self):
        self.assertTrue(self.coordination.claim("crew:c1", "card:1", "agent:a")["granted"])
        self.assertTrue(self.coordination.claim("crew:c1", "card:2", "agent:b")["granted"])

    def test_the_board_reports_who_holds_what(self):
        self.coordination.claim("crew:c1", "card:1", "agent:a")
        held = self.coordination.leases("crew:c1")
        self.assertEqual([(row["key"], row["holder"], row["live"]) for row in held],
                         [("card:1", "agent:a", True)])

    def test_a_stream_is_ordered_and_readable_from_a_cursor(self):
        for word in ("one", "two", "three"):
            self.coordination.append("channel:general", {"text": word})
        everything = self.coordination.read("channel:general")
        self.assertEqual([row["event"]["text"] for row in everything], ["one", "two", "three"])
        later = self.coordination.read("channel:general", since=everything[1]["seq"])
        self.assertEqual([row["event"]["text"] for row in later], ["three"])
        self.assertEqual(self.coordination.head("channel:general"), everything[-1]["seq"])


class DurableActorsPlanTests(AppCase):
    def test_self_hosting_needs_the_four_facts_their_guide_asks_for(self):
        with self.assertRaises(ValidationError) as caught:
            plan_runtime(self.settings, public_url="https://actors.example.com")
        self.assertIn("bucket", caught.exception.details["missing"])

    def test_the_plan_is_their_chart_with_your_values(self):
        plan = plan_runtime(self.settings, "https://actors.example.com", "state-bucket",
                            "actors@p.iam.gserviceaccount.com", "us-west4-a", version="1.2.3")
        values = plan.steps[0].content
        self.assertIn("publicUrl: https://actors.example.com", values)
        self.assertIn("bucket: state-bucket", values)
        self.assertIn("googleServiceAccount: actors@p.iam.gserviceaccount.com", values)
        self.assertIn("zone: us-west4-a", values)
        install = plan.steps[1].command
        self.assertEqual(install[:4], ["helm", "upgrade", "--install", "actors"])
        self.assertEqual(install[4], CHART.format(version="1.2.3"))
        self.assertEqual(plan.steps[2].command[-1], f"deployment/{SERVICE}")

    def test_the_plan_says_what_has_to_exist_first(self):
        plan = plan_runtime(self.settings, "https://a.example.com", "b", "g", "z")
        joined = " ".join(plan.notes)
        self.assertIn("actors-credentials", joined)
        self.assertIn("gvisor", joined)
        self.assertIn("DURABLE_ACTORS_INVOKE_API_KEY", joined)

    def test_the_actor_contract_covers_the_three_things_a_crew_needs(self):
        contract = actor_contract()
        self.assertEqual(sorted(contract["actors"]), ["AgentInbox", "ChannelRoom", "CrewBoard"])
        self.assertEqual(contract["actors"]["CrewBoard"]["maps_to"], "CoordinationBackend.claim")
        self.assertEqual(contract["actors"]["AgentInbox"]["maps_to"],
                         "CoordinationBackend.append/read")


if __name__ == "__main__":
    unittest.main()
