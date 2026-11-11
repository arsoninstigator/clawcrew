"""per-agent profile: the anti-mush tests.

each of these is a way configuration leaks between agents in a product like this, and
each one is meant to fail loudly rather than politely blend.
"""

from __future__ import annotations

import unittest

import base  # noqa: F401
from base import AppCase

from core.db import now
from core.errors import Conflict, NotAllowed, ValidationError


class IsolationTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.a = self.agent(self.team["id"], "wren", role="writer", skills=["drafting"])
        self.b = self.agent(self.team["id"], "okafor", role="editor", skills=["editing"])

    def test_memory_is_per_agent_even_for_the_same_key(self):
        self.app.profile.remember(self.a["id"], "house_style", "plain english")
        self.app.profile.remember(self.b["id"], "house_style", "oxford comma")
        self.assertEqual(self.app.profile.recall(self.a["id"], "house_style")["value"], "plain english")
        self.assertEqual(self.app.profile.recall(self.b["id"], "house_style")["value"], "oxford comma")
        self.assertEqual(self.app.profile.memory(self.a["id"])["count"], 1)
        self.app.profile.forget(self.a["id"], "house_style")
        with self.assertRaises(Exception):
            self.app.profile.recall(self.a["id"], "house_style")
        self.assertEqual(self.app.profile.recall(self.b["id"], "house_style")["value"], "oxford comma")

    def test_skills_and_tools_do_not_bleed(self):
        self.app.profile.set(self.a["id"], "tools", "browser", {"enabled": True})
        self.assertTrue(self.app.profile.tool_allowed(self.a["id"], "browser"))
        self.assertFalse(self.app.profile.tool_allowed(self.b["id"], "browser"))
        skills_a = set(self.app.profile.profile(self.a["id"])["namespaces"]["skills"])
        skills_b = set(self.app.profile.profile(self.b["id"])["namespaces"]["skills"])
        self.assertEqual(skills_a, {"drafting"})
        self.assertEqual(skills_b, {"editing"})

    def test_grants_are_per_agent_and_revocable(self):
        self.app.profile.grant(self.a["id"], "oauth", "google", scopes=["gmail.readonly"])
        self.assertEqual(len(self.app.profile.grants(self.a["id"])), 1)
        self.assertEqual(self.app.profile.grants(self.b["id"]), [])
        self.app.profile.revoke(self.a["id"], "oauth", "google")
        self.assertEqual(self.app.profile.grants(self.a["id"]), [])

    def test_an_expired_grant_stops_counting_and_is_flagged(self):
        self.app.profile.grant(
            self.a["id"], "apis", "stripe", scopes=["charges:read"], expires_at=now() - 60
        )
        self.assertEqual(self.app.profile.grants(self.a["id"]), [])
        self.assertEqual(len(self.app.profile.grants(self.a["id"], active_only=False)), 1)
        problems = self.app.profile.consistent(self.a["id"])["problems"]
        self.assertTrue(any("expired" in problem for problem in problems))

    def test_scope_updates_land_in_the_profile_not_the_legacy_column(self):
        self.crews.update_agent(self.a["id"], scope={"permissions": {"spend": True}})
        stored = self.app.profile.profile(self.a["id"])["namespaces"]["scope"]["permissions"]
        self.assertTrue(stored["spend"])
        row = self.app.store.one("select scope from agents where id = ?", (self.a["id"],))
        legacy = __import__("json").loads(row["scope"])
        self.assertFalse(legacy["permissions"]["spend"])  # the old column is not the source
        self.assertTrue(self.app.profile.can(self.a["id"], "spend"))
        self.assertFalse(self.app.profile.can(self.b["id"], "spend"))


class ConsistencyTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.a = self.agent(self.team["id"], "wren", role="writer")

    def test_a_value_replaces_and_never_deep_merges(self):
        self.app.profile.set(self.a["id"], "scope", "permissions", {"spend": True})
        stored = self.app.profile.profile(self.a["id"])["namespaces"]["scope"]["permissions"]
        self.assertEqual(stored, {"spend": True})  # not merged with the defaults
        # defaults still apply at read time, deliberately and visibly
        self.assertTrue(self.app.profile.can(self.a["id"], "dispatch_tasks"))

    def test_effective_reports_what_it_shadowed(self):
        defaults = {"scope": {"instructions": "be terse", "data_scope": "crew"}}
        self.app.profile.set(self.a["id"], "scope", "instructions", "be warm and specific")
        effective = self.app.profile.effective(self.a["id"], defaults=defaults)
        self.assertEqual(effective["namespaces"]["scope"]["instructions"], "be warm and specific")
        self.assertEqual(effective["namespaces"]["scope"]["data_scope"], "crew")
        self.assertEqual(
            effective["shadowed"]["scope.instructions"]["crew_default"], "be terse"
        )
        self.assertEqual(effective["sources"]["scope"]["instructions"], "agent")

    def test_fingerprint_tracks_change_and_holds_when_nothing_moves(self):
        first = self.app.profile.fingerprint(self.a["id"])
        self.assertEqual(first, self.app.profile.fingerprint(self.a["id"]))
        self.app.profile.set(self.a["id"], "instructions", "tone", "plain")
        second = self.app.profile.fingerprint(self.a["id"])
        self.assertNotEqual(first, second)

    def test_the_checks_that_can_be_proven(self):
        self.assertTrue(self.app.profile.consistent(self.a["id"])["ok"])  # a default vertical
        self.app.profile.delete(self.a["id"], "vertical", "primary")
        self.assertFalse(self.app.profile.consistent(self.a["id"])["ok"])  # no declared line of work
        self.app.profile.set(self.a["id"], "vertical", "primary", {"name": "editorial"})
        self.assertTrue(self.app.profile.consistent(self.a["id"])["ok"])
        self.app.profile.set(self.a["id"], "scope", "permissions", {"spend": "yes"})
        problems = self.app.profile.consistent(self.a["id"])["problems"]
        self.assertTrue(any("boolean" in problem for problem in problems))

    def test_unknown_namespace_and_bad_perm_key_are_refused(self):
        with self.assertRaises(ValidationError):
            self.app.profile.set(self.a["id"], "vibes", "x", 1)
        with self.assertRaises(ValidationError):
            self.app.profile.set(self.a["id"], "perms", "google", {"scopes": []})

    def test_isolation_report_says_whether_two_agents_are_the_same_agent(self):
        report = self.app.profile.isolation_report(self.team["id"])
        self.assertFalse(report["identical_profiles"])
        twin = self.crews.add_agent(self.team["id"], "clone", role="writer")
        for agent_id in (self.a["id"], twin["id"]):
            self.app.profile.set(agent_id, "vertical", "primary", {"name": "editorial"})
            self.app.profile.set(agent_id, "instructions", "tone", "plain")
            self.app.profile.set(agent_id, "skills", "drafting", {"enabled": True})
            try:
                self.app.profile.delete(agent_id, "skills", "research")
            except Exception:
                pass  # the twin never had it, which is the point of the test
        report = self.app.profile.isolation_report(self.team["id"])
        self.assertTrue(report["identical_profiles"])


class SpawnTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.a = self.agent(self.team["id"], "wren", role="writer")

    def test_spawning_is_off_until_it_is_turned_on(self):
        with self.assertRaises(NotAllowed):
            self.app.profile.spawn(self.a["id"], "helper")

    def test_a_child_starts_with_no_permissions_and_no_spawn_rights(self):
        self.app.profile.set_spawn_policy(self.a["id"], enabled=True)
        spawned = self.app.profile.spawn(self.a["id"], "helper", role="writer")
        child = spawned["child"]
        self.assertFalse(any(child["scope"]["permissions"].values()))
        self.assertFalse(self.app.profile.spawn_policy(child["id"])["enabled"])
        self.assertEqual(spawned["mode"], "ephemeral")
        self.assertEqual(self.crews.get_agent(self.a["id"])["scope"]["permissions"]["dispatch_tasks"], True)

    def test_limits_are_enforced(self):
        self.app.profile.set_spawn_policy(self.a["id"], enabled=True, max_children=1)
        self.app.profile.spawn(self.a["id"], "helper1", role="writer")
        with self.assertRaises(Conflict):
            self.app.profile.spawn(self.a["id"], "helper2", role="writer")

    def test_depth_is_enforced(self):
        self.app.profile.set_spawn_policy(self.a["id"], enabled=True, max_depth=1)
        child = self.app.profile.spawn(self.a["id"], "child", role="writer")["child"]
        self.app.profile.set_spawn_policy(child["id"], enabled=True, max_depth=1)
        with self.assertRaises(NotAllowed):
            self.app.profile.spawn(child["id"], "grandchild", role="writer")

    def test_role_gate_and_vertical_inheritance(self):
        self.app.profile.set(self.a["id"], "vertical", "primary", {"name": "editorial"})
        self.app.profile.set_spawn_policy(self.a["id"], enabled=True, allowed_roles=["researcher"])
        with self.assertRaises(NotAllowed):
            self.app.profile.spawn(self.a["id"], "helper", role="writer")
        spawned = self.app.profile.spawn(self.a["id"], "scout", role="researcher")
        vertical = self.app.profile.profile(spawned["child"]["id"])["namespaces"]["vertical"]
        self.assertEqual(vertical["primary"]["name"], "editorial")

    def test_lineage_and_retirement(self):
        self.app.profile.set_spawn_policy(self.a["id"], enabled=True)
        spawned = self.app.profile.spawn(self.a["id"], "helper", role="writer")
        child_id = spawned["child"]["id"]
        self.assertEqual(len(self.app.profile.children(self.a["id"])), 1)
        self.assertEqual(len(self.app.profile.lineage(child_id)), 1)
        self.app.profile.retire(child_id, reason="task done")
        self.assertEqual(self.app.profile.children(self.a["id"]), [])
        self.assertEqual(len(self.app.profile.children(self.a["id"], include_retired=True)), 1)
        actions = [entry["action"] for entry in self.app.audit.history(self.a["id"])]
        # a spawn is a subagent spawn now: the row names the lane, not just the child
        self.assertIn("subagent.spawned", actions)
        self.assertIn("agent.retired", actions)


if __name__ == "__main__":
    unittest.main()
