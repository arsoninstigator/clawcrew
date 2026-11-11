"""teams an agent works in, what it specialises in, and how much disk it gets.

the promises:
  one agent, several crews, with a home crew that owns its name
  specializations and verticals are per agent, and a spawn does not inherit them
  persistence is a real switch: task_state means no computer and no leftover browser
  browser access is open source on both sides, and refuses anything needing an account
"""

from __future__ import annotations

import unittest

import base  # noqa: F401
from base import AppCase

from sandbox import browser as browser_layer
from sandbox.browser.base import BrowserProvider, NullProvider, Session
from sandbox.browser.kernel_images import KernelImagesProvider
from core.errors import NotAllowed, ProviderNotConfigured, ValidationError


class MultiTeamTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.alpha = self.crew("alpha")
        self.beta = self.crew("beta")

    def test_an_agent_can_work_in_two_crews(self):
        agent = self.crews.add_agent(self.alpha["id"], "wren", teams=[self.beta["id"]])
        self.assertEqual(agent["home_team_id"], self.alpha["id"])
        self.assertEqual(set(agent["teams"]), {self.alpha["id"], self.beta["id"]})
        self.assertIn("wren", [a["name"] for a in self.crews.list_agents(self.beta["id"])])
        memberships = {t["team_id"]: t["membership"] for t in self.crews.teams_of(agent["id"])}
        self.assertEqual(memberships[self.alpha["id"]], "home")
        self.assertEqual(memberships[self.beta["id"]], "member")

    def test_joining_and_leaving_later(self):
        agent = self.crew_agent = self.crews.add_agent(self.alpha["id"], "okafor")
        joined = self.crews.join_team(agent["id"], self.beta["id"])
        self.assertEqual(len(joined["teams"]), 2)
        left = self.crews.leave_team(agent["id"], self.beta["id"])
        self.assertEqual(len(left["teams"]), 1)

    def test_the_home_crew_cannot_be_left_silently(self):
        agent = self.crews.add_agent(self.alpha["id"], "wren")
        with self.assertRaises(ValidationError):
            self.crews.leave_team(agent["id"], self.alpha["id"])

    def test_home_agents_are_listed_before_visiting_ones(self):
        self.crews.add_agent(self.beta["id"], "zeta")
        self.crews.add_agent(self.alpha["id"], "wren", teams=[self.beta["id"]])
        roster = [a["name"] for a in self.crews.list_agents(self.beta["id"])]
        self.assertEqual(roster[0], "zeta")
        home_only = [a["name"] for a in self.crews.list_agents(self.beta["id"],
                                                              include_members=False)]
        self.assertEqual(home_only, ["zeta"])


class SpecializationTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")

    def test_specializations_and_verticals_are_per_agent(self):
        one = self.crews.add_agent(self.team["id"], "wren",
                                   specializations=["search", "synthesis"],
                                   verticals=["health", "legal"])
        two = self.crews.add_agent(self.team["id"], "okafor", specializations=["editing"])
        self.assertEqual(one["specializations"], ["search", "synthesis"])
        self.assertEqual(one["verticals"], ["health", "legal"])
        self.assertEqual(two["specializations"], ["editing"])
        self.assertEqual(two["verticals"], [])

    def test_a_comma_string_is_accepted_because_that_is_how_people_type_it(self):
        agent = self.crews.add_agent(self.team["id"], "wren",
                                     specializations="search, synthesis, search")
        self.assertEqual(agent["specializations"], ["search", "synthesis"])

    def test_they_can_be_changed_without_touching_anything_else(self):
        agent = self.crews.add_agent(self.team["id"], "wren", specializations=["search"])
        updated = self.crews.set_specializations(agent["id"], verticals=["fintech"])
        self.assertEqual(updated["specializations"], ["search"])
        self.assertEqual(updated["verticals"], ["fintech"])

    def test_a_spawn_does_not_inherit_them(self):
        lead = self.crews.add_agent(self.team["id"], "lead", role="lead",
                                    specializations=["planning"], verticals=["health"])
        self.app.profile.set_spawn_policy(lead["id"], enabled=True, max_children=2, max_depth=2)
        spawn = self.app.profile.spawn(lead["id"], "lane", task_id="task_1",
                                       specializations=["reading"])
        self.assertEqual(spawn["child"]["specializations"], ["reading"])
        self.assertNotIn("planning", spawn["child"]["specializations"])


class PersistenceTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")

    def test_workspace_is_the_default_and_gets_a_computer(self):
        agent = self.crews.add_agent(self.team["id"], "wren")
        self.assertEqual(agent["persistence"], "workspace")
        self.assertEqual(agent["runtime"]["computer"], "persistent")

    def test_task_state_refuses_a_computer_outright(self):
        agent = self.crews.add_agent(self.team["id"], "scout", persistence="task_state")
        self.assertEqual(agent["persistence"], "task_state")
        with self.assertRaises(NotAllowed):
            self.crews.ensure_computer(agent["id"])

    def test_the_switch_moves_both_ways_and_is_validated(self):
        agent = self.crews.add_agent(self.team["id"], "scout")
        moved = self.crews.set_persistence(agent["id"], "task_state")
        self.assertEqual(moved["persistence"], "task_state")
        self.assertEqual(moved["runtime"]["computer"], "ephemeral")
        back = self.crews.set_persistence(agent["id"], "workspace")
        self.assertEqual(back["persistence"], "workspace")
        with self.assertRaises(ValidationError):
            self.crews.set_persistence(agent["id"], "sometimes")

    def test_a_task_state_agent_runs_in_a_throwaway_sandbox_not_a_computer(self):
        agent = self.crews.add_agent(self.team["id"], "scout", persistence="task_state")
        task = self.task(self.team["id"], "read everything", assignee_agent_id=agent["id"],
                         isolation="agent")
        provider, handle = self.runner._sandbox(self.crews.get_agent(agent["id"]), task)
        # 'agent' isolation would normally mean the persistent computer; task_state wins
        self.assertEqual(provider.name, "local")
        self.assertIsNone(self.crews.get_computer(agent["id"]))


class BrowserProviderTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")

    def test_both_lanes_are_open_source_and_need_no_account(self):
        providers = browser_layer.build_providers(self.settings)
        self.assertEqual(sorted(providers), ["browser-use", "kernel-images", "none"])
        for name in ("browser-use", "kernel-images"):
            provider = providers[name]
            self.assertFalse(provider.hosted)
            self.assertFalse(provider.needs_account)
        self.assertEqual(providers["browser-use"].kind, "driver")
        self.assertEqual(providers["kernel-images"].kind, "browser")

    def test_a_hosted_provider_subclass_is_refused(self):
        class Hosted(BrowserProvider):
            name = "cloud"
            hosted = True
            needs_account = True

            def ensure(self, key, spec=None):  # pragma: no cover - never reached
                raise AssertionError("should not be called")

            def stop(self, session):  # pragma: no cover
                return {}

        service = self.app.browsers
        service.providers["cloud"] = Hosted()
        with self.assertRaises(NotAllowed):
            service.provider("cloud")

    def test_sessions_are_rows_with_an_owner_and_a_scope(self):
        agent = self.crews.add_agent(self.team["id"], "wren")
        session = self.app.browsers.open(agent["id"])
        self.assertEqual(session["provider"], "browser-use")
        self.assertEqual(session["scope"], "persistent")
        self.assertEqual(session["agent_id"], agent["id"])
        self.assertEqual(len(self.app.browsers.list(agent["id"], state="ready")), 1)
        closed = self.app.browsers.close(session["id"])
        self.assertEqual(closed["state"], "closed")

    def test_a_task_state_agent_gets_a_task_scoped_browser(self):
        agent = self.crews.add_agent(self.team["id"], "scout", persistence="task_state")
        session = self.app.browsers.open(agent["id"])
        self.assertEqual(session["scope"], "task")
        self.assertNotEqual(session["profile_dir"], "")

    def test_missing_driver_says_what_to_install_instead_of_falling_back_to_cloud(self):
        agent = self.crews.add_agent(self.team["id"], "wren")
        try:
            self.app.browsers.run(agent["id"], "find the number of stars")
        except ProviderNotConfigured as exc:
            self.assertIn("browser-use", str(exc))
            self.assertIn("pip install", str(exc))
        else:  # pragma: no cover - only when browser-use happens to be installed
            self.skipTest("browser-use is installed; the driver path ran for real")

    def test_kernel_images_without_an_image_explains_how_to_build_one(self):
        provider = KernelImagesProvider(settings=self.settings)
        with self.assertRaises(ProviderNotConfigured) as caught:
            provider.ensure("agent_x:task")
        self.assertIn("kernel-images", str(caught.exception))
        self.assertIn("apache-2.0", str(caught.exception))

    def test_kernel_images_attaches_to_a_browser_you_started_yourself(self):
        settings = type("S", (), {"kernel_cdp_url": "ws://127.0.0.1:9222/devtools/browser/abc",
                                  "kernel_images_image": ""})()
        provider = KernelImagesProvider(settings=settings)
        session = provider.ensure("agent_x:task", {"scope": "task"})
        self.assertEqual(session.cdp_url, "ws://127.0.0.1:9222/devtools/browser/abc")
        self.assertEqual(session.handle["mode"], "attach")
        self.assertFalse(provider.stop(session)["stopped"])

    def test_kernel_images_runs_the_local_container_and_waits_for_cdp(self):
        calls: list[list[str]] = []

        def runner(argv):
            calls.append(argv)
            if argv[:2] == ["docker", "run"]:
                return 0, "container123\n", ""
            if argv[:2] == ["docker", "port"]:
                return 0, "127.0.0.1:43210\n", ""
            if argv[:2] == ["docker", "stop"]:
                return 0, "container123\n", ""
            return 1, "", "unexpected"

        settings = type("S", (), {"kernel_cdp_url": "", "kernel_images_image": "kernel-docker"})()
        provider = KernelImagesProvider(settings=settings, runner=runner)
        provider._wait_for_cdp = lambda port, timeout: f"ws://127.0.0.1:{port}/devtools/browser/x"
        session = provider.ensure("agent_x:task", {"scope": "task"})
        self.assertEqual(session.cdp_url, "ws://127.0.0.1:43210/devtools/browser/x")
        self.assertEqual(session.handle["container"], "container123")
        self.assertIn("-p", calls[0])
        self.assertIn("127.0.0.1::9222", calls[0])
        stopped = provider.stop(session)
        self.assertTrue(stopped["stopped"])
        self.assertEqual(calls[-1][:3], ["docker", "stop", "container123"])

    def test_none_provider_refuses_politely(self):
        provider = NullProvider()
        with self.assertRaises(NotAllowed):
            provider.ensure("agent_x:task")
        self.assertFalse(provider.status()["ready"])


class FakeDriver(BrowserProvider):
    name = "fake-driver"
    kind = "driver"

    def __init__(self):
        self.ran: list[tuple[str, str]] = []

    def ensure(self, key, spec=None):
        return Session(id=f"fake-{key}", provider=self.name,
                       scope=(spec or {}).get("scope", "task"), cdp_url="ws://fake")

    def stop(self, session):
        return {"stopped": True}

    def run(self, session, task, llm="", **options):
        self.ran.append((session.id, task))
        return {"provider": self.name, "result": f"did: {task}", "model": llm}


class BrowserRunTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.agent = self.crews.add_agent(self.team["id"], "wren", model="claude-sonnet-5")
        self.driver = FakeDriver()
        self.app.browsers.providers["fake-driver"] = self.driver

    def test_a_run_opens_a_session_and_records_it_before_driving(self):
        out = self.app.browsers.run(self.agent["id"], "read the page",
                                    spec={"provider": "fake-driver"})
        self.assertEqual(out["result"], "did: read the page")
        self.assertEqual(out["model"], "claude-sonnet-5")
        self.assertEqual(len(self.app.browsers.list(self.agent["id"])), 1)
        self.assertEqual(self.driver.ran[0][1], "read the page")

    def test_a_run_can_reuse_an_open_session(self):
        session = self.app.browsers.open(self.agent["id"], {"provider": "fake-driver"})
        self.app.browsers.run(self.agent["id"], "second turn", session_id=session["id"])
        self.assertEqual(len(self.app.browsers.list(self.agent["id"])), 1)

    def test_a_browser_only_provider_cannot_drive(self):
        self.app.browsers.providers["kernel-images"] = KernelImagesProvider(
            settings=type("S", (), {"kernel_cdp_url": "ws://127.0.0.1:9222/x",
                                    "kernel_images_image": ""})()
        )
        session = self.app.browsers.open(self.agent["id"], {"provider": "kernel-images"})
        with self.assertRaises(ProviderNotConfigured):
            self.app.browsers.run(self.agent["id"], "go", session_id=session["id"])

    def test_an_empty_task_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.app.browsers.run(self.agent["id"], "   ")


class SurfaceTests(AppCase):
    """the routes a frontend needs for identity, teams, attestation and browser."""

    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient

        from api.routes import create_api

        self.client = TestClient(create_api(self.app))
        self.team = self.crew("shop")
        self.agent = self.crews.add_agent(self.team["id"], "wren", role="research",
                                          specializations=["search"], verticals=["health"])

    def test_identity_routes(self):
        self.assertEqual(self.client.get(f"/agents/{self.agent['id']}/principal").status_code, 200)
        by_id = self.client.get(f"/agents/by-agentid/{self.agent['agentid']}")
        self.assertEqual(by_id.status_code, 200)
        self.assertEqual(by_id.json()["id"], self.agent["id"])
        bundle = self.client.get(f"/agents/{self.agent['id']}/attestation").json()
        self.assertEqual(bundle["kind"], "clawcrew.attestation")

    def test_a_bundle_verifies_through_the_api(self):
        self.app.audit.record(team_id=self.team["id"], agent_id=self.agent["id"],
                              action="agent.created")
        bundle = self.client.get(f"/agents/{self.agent['id']}/attestation").json()
        checked = self.client.post("/attestations/verify", json={"entries": bundle["entries"]})
        self.assertTrue(checked.json()["ok"])

    def test_team_and_specialization_routes(self):
        other = self.crew("beta")
        joined = self.client.post(f"/crews/{other['id']}/agents/{self.agent['id']}", json={})
        self.assertEqual(joined.status_code, 200)
        teams = self.client.get(f"/agents/{self.agent['id']}/teams").json()["teams"]
        self.assertEqual(len(teams), 2)
        changed = self.client.post(f"/agents/{self.agent['id']}/specializations",
                                   json={"verticals": ["fintech"]}).json()
        self.assertEqual(changed["verticals"], ["fintech"])
        self.assertEqual(changed["specializations"], ["search"])

    def test_persistence_and_browser_routes(self):
        self.assertEqual(self.client.post(f"/agents/{self.agent['id']}/persistence",
                                          json={"mode": "task_state"}).status_code, 200)
        session = self.client.post(f"/agents/{self.agent['id']}/browser", json={}).json()
        self.assertEqual(session["scope"], "task")
        listed = self.client.get(f"/agents/{self.agent['id']}/browser").json()["sessions"]
        self.assertEqual(len(listed), 1)
        closed = self.client.delete(f"/browser/sessions/{session['id']}").json()
        self.assertEqual(closed["state"], "closed")
        providers = self.client.get("/browser/providers").json()["providers"]
        self.assertTrue(all(not p["hosted"] for p in providers))

    def test_a_missing_driver_is_503_with_the_install_line(self):
        response = self.client.post(f"/agents/{self.agent['id']}/browser/run",
                                    json={"task": "read the page"})
        if response.status_code == 200:  # pragma: no cover - browser-use is installed
            self.skipTest("browser-use installed")
        self.assertEqual(response.status_code, 503)
        self.assertIn("browser-use", response.json()["message"])


if __name__ == "__main__":
    unittest.main()
