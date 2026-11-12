"""firecrawl, called not copied.

a toolbox entry is the smallest thing that can be called: a status that says what is
missing, one method per call, a plain dict back, and a failure that carries the status code
so the caller does not have to string-match. no import of the project's own code, which is
also what keeps an agpl-3.0 tool at arm's length from an apache-2.0 backend.

    scrape  one page, to markdown by default
    crawl   a site, bounded by a page limit
    map     the urls of a site, without fetching them

crawl and map can answer with a job id rather than a result. that is reported as
`{"pending": true, "id": ...}` instead of an empty `data` the caller would read as "nothing
there", which is the bug this shape exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from core.errors import ProviderFailure, ProviderNotConfigured

DEFAULT_BASE = "https://api.firecrawl.dev/v1"


@dataclass
class FirecrawlClient:
    api_key: str = ""
    base_url: str = DEFAULT_BASE
    timeout: float = 120.0
    transport: httpx.BaseTransport | None = None

    # ---- shape every toolbox entry has ---------------------------------
    def configured(self) -> bool:
        return bool(self.api_key)

    def status(self) -> dict:
        return {
            "kind": "firecrawl",
            "called": True,
            "base_url": self.base_url,
            "configured": self.configured(),
            "needs": "CRITTERTOWN_FIRECRAWL_API_KEY",
            "calls": ["scrape", "crawl", "map"],
        }

    # ---- transport -----------------------------------------------------
    def _client(self) -> httpx.Client:
        if not self.configured():
            raise ProviderNotConfigured(
                "firecrawl has no api key",
                provider="firecrawl",
                hint="set CRITTERTOWN_FIRECRAWL_API_KEY, or leave it unset and stop calling it",
            )
        return httpx.Client(
            base_url=(self.base_url or DEFAULT_BASE).strip().rstrip("/"),
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
            transport=self.transport,
        )

    def _post(self, path: str, body: dict, what: str) -> dict:
        with self._client() as client:
            try:
                response = client.post(path, json=body)
            except httpx.HTTPError as exc:
                raise ProviderFailure(f"firecrawl {what} did not answer: {exc}", provider="firecrawl") from exc
        if response.status_code >= 400:
            raise ProviderFailure(
                f"firecrawl {what} returned {response.status_code}",
                provider="firecrawl",
                status=response.status_code,
                body=response.text[:500],
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderFailure(f"firecrawl {what} returned non-json", body=response.text[:500]) from exc
        if not isinstance(payload, dict):
            raise ProviderFailure(f"firecrawl {what} returned {type(payload).__name__}, not an object")
        return payload

    @staticmethod
    def _result(payload: dict) -> dict:
        """a job id instead of a result is pending, not empty."""
        data = payload.get("data")
        if data is None and payload.get("id"):
            return {"pending": True, "id": payload["id"], "success": payload.get("success", True)}
        return data if isinstance(data, dict) else payload

    # ---- the calls -----------------------------------------------------
    def scrape(self, url: str, formats: list[str] | None = None, only_main_content: bool = True) -> dict:
        payload = self._post(
            "/scrape",
            {"url": url, "formats": formats or ["markdown"], "onlyMainContent": only_main_content},
            f"scrape of {url}",
        )
        return self._result(payload)

    def crawl(self, url: str, limit: int = 10, formats: list[str] | None = None) -> dict:
        payload = self._post(
            "/crawl",
            {"url": url, "limit": max(1, int(limit)), "scrapeOptions": {"formats": formats or ["markdown"]}},
            f"crawl of {url}",
        )
        return self._result(payload)

    def map(self, url: str, search: str = "", limit: int = 100) -> dict:
        body = {"url": url, "limit": max(1, int(limit))}
        if search:
            body["search"] = search
        return self._post("/map", body, f"map of {url}")


def make(api_key: str = "", base_url: str = DEFAULT_BASE, transport=None, timeout: float = 120.0):
    return FirecrawlClient(api_key=api_key, base_url=base_url, transport=transport, timeout=timeout)
