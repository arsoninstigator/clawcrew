"""the served shell, held to the api it renders.

a page is the easiest thing in a repo to let rot, because nothing imports it. these
tests do the two things a page needs: it renders, and every route it calls exists.
"""

from __future__ import annotations

import re
import unittest

from base import AppCase

from api.pages import ISOLATION_PAGE, POV_PAGE
from api.routes import create_api

ROUTE_TABLE = re.compile(r"var R = \{(.*?)\};", re.S)
ENTRY = re.compile(r'(\w+):\s*"([^"]+)"')

# the only writes the shell is allowed: a card move, a calendar block, a profile value,
# a revoked grant, closing a routine's receipt, pausing a routine, and pausing a limit.
# everything else it renders is read-only by construction.
WRITES = {"moveTask", "calendar", "profileEntry", "grant", "routineDone", "routinePause",
          "materialise", "guardrail"}


def routes_in(page: str) -> dict[str, str]:
    table = ROUTE_TABLE.search(page)
    assert table, "the page has no route table"
    return dict(ENTRY.findall(table.group(1)))


def shape(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


class PageShapeTests(unittest.TestCase):
    def test_the_shell_carries_a_route_table(self):
        routes = routes_in(POV_PAGE)
        self.assertGreaterEqual(len(routes), 12)
        self.assertIn("agents/{agent}/profile/{namespace}/{key}", routes["profileEntry"])

    def test_the_nine_namespaces_are_all_in_the_editor(self):
        namespaces = re.search(r"var NAMESPACES = \[(.*?)\];", POV_PAGE, re.S)
        names = re.findall(r'"(\w+)"', namespaces.group(1))
        self.assertEqual(
            names,
            ["skills", "scope", "memory", "instructions", "knowledge", "perms",
             "tools", "vertical", "spawn"],
        )

    def test_the_page_writes_only_where_it_is_allowed_to(self):
        methods = re.findall(r'method:\s*"(\w+)"', POV_PAGE)
        self.assertEqual(sorted(methods),
                         ["DELETE", "DELETE", "POST", "POST", "POST", "POST", "POST", "PUT"])
        for name in WRITES:
            self.assertIn(name, routes_in(POV_PAGE))

    def test_the_new_views_are_in_the_shell(self):
        self.assertIn('"routines","guardrails"', POV_PAGE)
        self.assertIn("a missed routine reads as missed", POV_PAGE)
        self.assertIn("refused today", POV_PAGE)

    def test_a_save_shows_the_value_it_stores_not_a_merge(self):
        self.assertIn("there is no merged preview", POV_PAGE)
        self.assertIn("JSON.parse(box.value)", POV_PAGE)

    def test_the_header_shows_the_key_beside_the_name(self):
        self.assertIn("agentKey", POV_PAGE)
        self.assertIn("agent_key", POV_PAGE)

    def test_isolation_is_its_own_page(self):
        self.assertIn("identical_profiles", ISOLATION_PAGE)
        self.assertIn("crews/{team}/isolation", ISOLATION_PAGE)


class PageRouteTests(AppCase):
    def test_every_route_in_the_shell_exists(self):
        api = create_api(self.app)
        known = {shape(getattr(route, "path", "")) for route in api.routes}
        for name, path in routes_in(POV_PAGE).items():
            self.assertIn(shape("/" + path), known, f"the shell calls {name}: /{path}")

    def test_every_route_in_the_isolation_page_exists(self):
        api = create_api(self.app)
        known = {shape(getattr(route, "path", "")) for route in api.routes}
        for name, path in routes_in(ISOLATION_PAGE).items():
            self.assertIn(shape("/" + path), known, f"the page calls {name}: /{path}")

    def test_the_pages_are_served(self):
        from fastapi.testclient import TestClient

        client = TestClient(create_api(self.app))
        pov = client.get("/ui/pov")
        self.assertEqual(pov.status_code, 200)
        self.assertIn("crittertown · pov", pov.text)
        isolation = client.get("/ui/isolation")
        self.assertEqual(isolation.status_code, 200)
        self.assertIn("crittertown · isolation", isolation.text)

    def test_the_shell_renders_a_real_crew(self):
        from fastapi.testclient import TestClient

        team = self.crew("shop")
        agent = self.agent(team["id"], "wren", role="writer", skills=["writing"])
        self.kanban.create_task(team["id"], "publish the post", column="ready",
                                assignee_agent_id=agent["id"])
        client = TestClient(create_api(self.app))
        board = client.get(f"/agents/{agent['id']}/board").json()
        self.assertEqual(board["total"], 1)
        self.assertTrue(any(c["name"] == "ready" for c in board["columns"]))
        profile = client.get(f"/agents/{agent['id']}/profile").json()
        self.assertIn("skills", profile["namespaces"])
        self.assertTrue(profile["fingerprint"])

    def test_a_namespace_write_replaces_rather_than_merges(self):
        from fastapi.testclient import TestClient

        team = self.crew("shop")
        agent = self.agent(team["id"], "wren", role="writer", skills=["writing"])
        client = TestClient(create_api(self.app))
        client.put(f"/agents/{agent['id']}/profile/instructions/voice",
                   json={"value": {"tone": "plain"}, "origin": "owner"})
        client.put(f"/agents/{agent['id']}/profile/instructions/voice",
                   json={"value": {"tone": "warm"}, "origin": "owner"})
        stored = client.get(f"/agents/{agent['id']}/profile").json()
        self.assertEqual(stored["namespaces"]["instructions"]["voice"], {"tone": "warm"})
        self.assertTrue(client.get(f"/agents/{agent['id']}/consistency").json()["ok"])


if __name__ == "__main__":
    unittest.main()
