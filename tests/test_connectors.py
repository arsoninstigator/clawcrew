"""workspace knowledge: adapters read, supervisors decide, agents get refused."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

import httpx

from base import AppCase

from clawcrew.manage.knowledge.service import build_adapter
from clawcrew.errors import NotAllowed, ProviderNotConfigured, ValidationError


class FolderAdapterTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("shop")
        self.wren = self.agent(self.team["id"], "wren", role="writer", skills=["writing"])
        self.docs = tempfile.mkdtemp(prefix="clawcrew-docs-")
        with open(os.path.join(self.docs, "pricing.md"), "w", encoding="utf-8") as fh:
            fh.write("# pricing\n\nstarter is 20 a month.\n")
        with open(os.path.join(self.docs, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("call the supplier on friday")

    def connect(self):
        return self.app.connectors.connect(self.team["id"], "folder", "handbook",
                                           config={"path": self.docs})

    def test_sync_reads_the_folder_once_and_updates_in_place(self):
        connector = self.connect()
        first = self.app.connectors.sync(connector["id"])
        self.assertEqual(first["added"], 2)
        self.assertEqual(first["updated"], 0)
        second = self.app.connectors.sync(connector["id"])
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["total"], 2)
        with open(os.path.join(self.docs, "pricing.md"), "a", encoding="utf-8") as fh:
            fh.write("\ngrowth is 40.\n")
        third = self.app.connectors.sync(connector["id"])
        self.assertEqual(third["updated"], 1)

    def test_an_agent_without_access_is_refused_not_lied_to(self):
        connector = self.connect()
        self.app.connectors.sync(connector["id"])
        with self.assertRaises(NotAllowed) as ctx:
            self.app.connectors.search(self.wren["id"], "pricing", connector_id=connector["id"])
        self.assertIn("supervisor", ctx.exception.details.get("ask", ""))
        # and with no connector named, the search simply has nothing to read
        found = self.app.connectors.search(self.wren["id"], "pricing")
        self.assertEqual(found["hits"], [])
        self.assertEqual(found["denied"], [connector["id"]])

    def test_a_supervisor_grants_and_revokes_within_its_own_department(self):
        self.okafor = self.agent(self.team["id"], "okafor", role="research")
        department = self.app.departments.create(
            self.team["id"], "content", coordinator_agent_id=self.wren["id"],
            members=[self.okafor["id"]])
        self.assertTrue(department["id"])
        connector = self.connect()
        self.app.connectors.sync(connector["id"])
        self.app.connectors.grant(self.okafor["id"], connector["id"],
                                  supervisor_id=self.wren["id"], expires_hours=2)
        found = self.app.connectors.search(self.okafor["id"], "supplier")
        self.assertEqual(len(found["hits"]), 1)
        self.assertEqual(found["hits"][0]["title"], "notes")
        ledger = self.app.connectors.access(supervisor_id=self.wren["id"])
        self.assertEqual(len(ledger["grants"]), 1)
        self.assertEqual(ledger["grants"][0]["agent_id"], self.okafor["id"])
        self.app.connectors.revoke(self.okafor["id"], connector["id"],
                                   supervisor_id=self.wren["id"], reason="project over")
        self.assertEqual(self.app.connectors.search(self.okafor["id"], "supplier")["hits"], [])

    def test_an_agent_that_is_not_a_supervisor_cannot_grant(self):
        connector = self.connect()
        with self.assertRaises(NotAllowed):
            self.app.connectors.grant(self.wren["id"], connector["id"],
                                      supervisor_id=self.wren["id"])

    def test_a_supervisor_cannot_reach_outside_its_department(self):
        outsider = self.agent(self.team["id"], "outsider", role="design")
        supervisor = self.agent(self.team["id"], "head", role="lead")
        self.app.departments.create(self.team["id"], "content",
                                    coordinator_agent_id=supervisor["id"],
                                    members=[self.wren["id"]])
        connector = self.connect()
        with self.assertRaises(NotAllowed):
            self.app.connectors.grant(outsider["id"], connector["id"],
                                      supervisor_id=supervisor["id"])

    def test_a_grant_expires(self):
        connector = self.connect()
        self.app.connectors.sync(connector["id"])
        self.app.connectors.grant(self.wren["id"], connector["id"], expires_hours=-1)
        self.assertFalse(self.app.connectors.allowed(self.wren["id"], connector["id"]))
        self.assertEqual(self.app.connectors.search(self.wren["id"], "pricing")["hits"], [])

    def test_a_missing_path_is_loud(self):
        connector = self.app.connectors.connect(
            self.team["id"], "folder", "gone", config={"path": "/nope/never"})
        self.assertEqual(connector["status"], "unverified")
        with self.assertRaises(ProviderNotConfigured):
            self.app.connectors.sync(connector["id"])


class AdapterShapeTests(unittest.TestCase):
    def test_notion_search_is_parsed_into_documents(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/search"):
                return httpx.Response(200, json={"results": [{
                    "id": "page-1", "url": "https://notion.so/page-1",
                    "last_edited_time": "2026-11-01T00:00:00Z",
                    "properties": {"title": {"type": "title", "title": [
                        {"plain_text": "launch plan"}]}},
                }]})
            return httpx.Response(200, json={"results": [{"paragraph": {"rich_text": [
                {"plain_text": "ship on friday"}]}}]})

        adapter = build_adapter("notion", {}, token="t", transport=httpx.MockTransport(handler))
        docs = adapter.documents(limit=5)
        self.assertEqual(docs[0]["title"], "launch plan")
        self.assertEqual(docs[0]["text"], "ship on friday")
        self.assertEqual(docs[0]["external_id"], "page-1")

    def test_http_adapter_carries_any_documents_endpoint(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"documents": [
                {"id": "1", "title": "runbook", "text": "restart the worker", "url": "x"}]})

        adapter = build_adapter("http", {"url": "https://wiki.local/docs"},
                                transport=httpx.MockTransport(handler))
        self.assertEqual(adapter.probe()["documents"], 1)
        self.assertEqual(adapter.search("worker")[0]["title"], "runbook")

    def test_unknown_kind_is_a_validation_error(self):
        with self.assertRaises(ValidationError):
            build_adapter("salesforce", {})


if __name__ == "__main__":
    unittest.main()
