"""dify, self-hosted.

dify publishes a service api per app: `POST /v1/workflows/run` with a bearer app key,
`response_mode: "blocking"` for a synchronous result and `"streaming"` for server
sent events. `GET /v1/info` and `GET /v1/parameters` tell you which app you are
pointed at and which inputs it accepts, which is what a workflow node needs to fail
early instead of mid-run.

`base_url` is the instance root; `/v1` is added if it is missing, because a pasted
`http://localhost/v1` and `http://localhost` should both work.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..errors import ProviderFailure, ProviderNotConfigured

DEFAULT_BASE = "http://localhost/v1"


def _root(base_url: str) -> str:
    root = (base_url or DEFAULT_BASE).strip().rstrip("/")
    return root if root.endswith("/v1") else root + "/v1"


@dataclass
class DifyClient:
    base_url: str = DEFAULT_BASE
    api_key: str = ""
    user: str = "clawcrew"
    timeout: float = 120.0
    transport: httpx.BaseTransport | None = None

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def status(self) -> dict:
        return {
            "kind": "dify",
            "self_hosted": True,
            "base_url": _root(self.base_url),
            "configured": self.configured(),
            "needs": "CLAWCREW_DIFY_API_KEY (a dify app key, starts with app-)",
        }

    def _client(self) -> httpx.Client:
        if not self.configured():
            raise ProviderNotConfigured(
                "no dify app key",
                provider="dify",
                hint="run the dify docker compose, open the app, then API Access > create key",
            )
        return httpx.Client(
            base_url=_root(self.base_url),
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
            timeout=self.timeout,
            transport=self.transport,
        )

    def _call(self, method: str, path: str, what: str, **kwargs):
        with self._client() as client:
            try:
                response = client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise ProviderFailure(f"dify unreachable: {what}", error=str(exc)) from exc
        if response.status_code >= 400:
            raise ProviderFailure(
                f"dify {what} failed", status=response.status_code, body=response.text[:2000]
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderFailure(f"dify {what} returned non-json", body=response.text[:500]) from exc

    def info(self) -> dict:
        return self._call("GET", "/info", "reading app info")

    def parameters(self) -> dict:
        return self._call("GET", "/parameters", "reading app parameters")

    def run_workflow(
        self,
        inputs: dict | None = None,
        user: str | None = None,
        response_mode: str = "blocking",
        timeout: float | None = None,
    ) -> dict:
        """run the app's workflow and return dify's `data` block, which carries
        `status`, `outputs`, `error`, `elapsed_time` and token counts."""
        body = {
            "inputs": inputs or {},
            "response_mode": response_mode,
            "user": user or self.user,
        }
        payload = self._call(
            "POST", "/workflows/run", "running a workflow", json=body,
            timeout=timeout or self.timeout,
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else payload

    def stop(self, task_id: str, user: str | None = None) -> dict:
        return self._call(
            "POST", f"/workflows/tasks/{task_id}/stop", "stopping a workflow",
            json={"user": user or self.user},
        )


def make(base_url: str = DEFAULT_BASE, api_key: str = "", user: str = "clawcrew",
         transport=None, timeout: float = 120.0):
    return DifyClient(base_url=base_url, api_key=api_key, user=user, transport=transport, timeout=timeout)
