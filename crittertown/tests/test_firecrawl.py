"""the worked example of a toolbox entry.

firecrawl is called, never copied: these tests assert the requests crittertown makes to it
with a mock transport, and the two things a toolbox entry has to get right. it says what is
missing instead of failing obscurely, and a job id comes back as `pending` rather than as an
empty result a caller would read as "nothing there".
"""

from __future__ import annotations

import json
import unittest

import httpx

import base  # noqa: F401

from core.config import Settings
from core.errors import ProviderFailure, ProviderNotConfigured
from core.services import build_services
from toolbox import firecrawl


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(request.read())
        status, payload = self.responses.pop(0)
        return httpx.Response(status, json=payload)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def json(self, index: int = 0) -> dict:
        return json.loads(self.bodies[index] or b"{}")


class FirecrawlTests(unittest.TestCase):
    def client(self, recorder: Recorder) -> firecrawl.FirecrawlClient:
        return firecrawl.make("fc_key", "https://crawl.internal/v1", transport=recorder.transport)

    def test_scrape_posts_the_page_and_returns_the_data_block(self):
        recorder = Recorder((200, {"success": True, "data": {"markdown": "# hello"}}))
        result = self.client(recorder).scrape("https://example.com")
        request = recorder.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/v1/scrape")
        self.assertEqual(request.headers["authorization"], "Bearer fc_key")
        self.assertEqual(recorder.json(), {"url": "https://example.com", "formats": ["markdown"],
                                           "onlyMainContent": True})
        self.assertEqual(result["markdown"], "# hello")

    def test_a_job_id_is_pending_not_empty(self):
        recorder = Recorder((200, {"success": True, "id": "crawl_7"}))
        result = self.client(recorder).crawl("https://example.com", limit=5)
        self.assertTrue(result["pending"])
        self.assertEqual(result["id"], "crawl_7")
        self.assertEqual(recorder.json()["limit"], 5)

    def test_a_bad_key_is_a_provider_failure_with_the_status(self):
        recorder = Recorder((401, {"error": "unauthorized"}))
        with self.assertRaises(ProviderFailure) as ctx:
            self.client(recorder).scrape("https://example.com")
        self.assertEqual(ctx.exception.details.get("status"), 401)

    def test_an_unconfigured_entry_says_what_is_missing(self):
        client = firecrawl.make("")
        self.assertFalse(client.configured())
        self.assertIn("CRITTERTOWN_FIRECRAWL_API_KEY", client.status()["needs"])
        with self.assertRaises(ProviderNotConfigured) as ctx:
            client.scrape("https://example.com")
        self.assertIn("CRITTERTOWN_FIRECRAWL_API_KEY", ctx.exception.details.get("hint", ""))

    def test_map_skips_the_search_term_when_there_is_none(self):
        recorder = Recorder((200, {"success": True, "links": ["https://example.com/a"]}))
        result = self.client(recorder).map("https://example.com")
        self.assertEqual(result["links"], ["https://example.com/a"])
        self.assertNotIn("search", recorder.json())

    def test_the_registry_carries_it_like_any_other_service(self):
        services = build_services(Settings.from_env({}))
        self.assertIn("firecrawl", services.status())
        self.assertFalse(services.status()["firecrawl"]["configured"])


if __name__ == "__main__":
    unittest.main()
