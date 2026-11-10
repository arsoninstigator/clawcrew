"""n8n, self-hosted.

two apis, because n8n has two:

* the **public REST api** at `/api/v1` authenticated with `X-N8N-API-KEY`, for
  managing workflows (list, get, create, activate, deactivate) and reading
  executions. this api only exists on a self-hosted instance.
* the **webhook** a workflow exposes at `/webhook/<path>` (or `/webhook-test/<path>`
  while editing), which is how work actually gets triggered with a payload.

a webhook node set to respond "when last node finishes" answers synchronously with
its own result, so `trigger()` returns that result instead of making the caller poll
executions. when a workflow answers immediately, `trigger()` says so and the caller
should use `executions()`.

nothing from n8n's source is used here; this is http against your own instance.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from clawcrew.errors import ProviderFailure, ProviderNotConfigured

DEFAULT_BASE = "http://localhost:5678"
API_PREFIX = "/api/v1"


def _root(base_url: str) -> str:
    """accept `http://h:5678`, `http://h:5678/`, or a pasted `.../api/v1`."""
    root = (base_url or DEFAULT_BASE).strip().rstrip("/")
    if root.endswith(API_PREFIX):
        root = root[: -len(API_PREFIX)]
    return root


@dataclass
class N8NClient:
    base_url: str = DEFAULT_BASE
    api_key: str = ""
    timeout: float = 60.0
    transport: httpx.BaseTransport | None = None

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def status(self) -> dict:
        return {
            "kind": "n8n",
            "self_hosted": True,
            "base_url": _root(self.base_url),
            "configured": self.configured(),
            "needs": "CLAWCREW_N8N_API_KEY (Settings > n8n API in your instance)",
        }

    def _client(self) -> httpx.Client:
        if not self.configured():
            raise ProviderNotConfigured(
                "no n8n api key",
                provider="n8n",
                hint="create one in your self-hosted n8n under Settings > n8n API",
            )
        return httpx.Client(
            base_url=_root(self.base_url),
            headers={"X-N8N-API-KEY": self.api_key, "Accept": "application/json"},
            timeout=self.timeout,
            transport=self.transport,
        )

    # ---- rest api -----------------------------------------------------
    def _call(self, method: str, path: str, what: str, **kwargs):
        with self._client() as client:
            try:
                response = client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                raise ProviderFailure(f"n8n unreachable: {what}", error=str(exc)) from exc
        if response.status_code >= 400:
            raise ProviderFailure(
                f"n8n {what} failed", status=response.status_code, body=response.text[:2000]
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderFailure(f"n8n {what} returned non-json", body=response.text[:500]) from exc

    def list_workflows(self, active: bool | None = None, limit: int = 100) -> list[dict]:
        query = {"limit": max(1, min(int(limit), 250))}
        if active is not None:
            query["active"] = "true" if active else "false"
        payload = self._call("GET", f"{API_PREFIX}/workflows", "listing workflows", params=query)
        return payload.get("data") or []

    def get_workflow(self, workflow_id: str) -> dict:
        return self._call("GET", f"{API_PREFIX}/workflows/{workflow_id}", "reading a workflow")

    def create_workflow(self, definition: dict, name: str | None = None) -> dict:
        body = {
            "name": name or definition.get("name") or "clawcrew",
            "nodes": definition.get("nodes") or [],
            "connections": definition.get("connections") or {},
            "settings": definition.get("settings") or {},
        }
        return self._call("POST", f"{API_PREFIX}/workflows", "creating a workflow", json=body)

    def activate(self, workflow_id: str) -> dict:
        return self._call(
            "POST", f"{API_PREFIX}/workflows/{workflow_id}/activate", "activating a workflow"
        )

    def deactivate(self, workflow_id: str) -> dict:
        return self._call(
            "POST", f"{API_PREFIX}/workflows/{workflow_id}/deactivate", "deactivating a workflow"
        )

    def executions(self, workflow_id: str | None = None, limit: int = 20) -> list[dict]:
        query: dict = {"limit": max(1, min(int(limit), 250))}
        if workflow_id:
            query["workflowId"] = workflow_id
        payload = self._call("GET", f"{API_PREFIX}/executions", "listing executions", params=query)
        return payload.get("data") or []

    def export_workflow(self, workflow_id: str) -> dict:
        """the shape `clawcrew.orgchart.authoring.importers.from_n8n` already understands, so a
        workflow running on your instance can be brought into a clawcrew dag."""
        workflow = self.get_workflow(workflow_id)
        return {
            "name": workflow.get("name"),
            "nodes": workflow.get("nodes") or [],
            "connections": workflow.get("connections") or {},
        }

    # ---- webhook ------------------------------------------------------
    def trigger(self, path: str, payload: dict | None = None, test: bool = False,
                timeout: float | None = None) -> dict:
        """POST the payload at the workflow's webhook. `test=True` hits
        `/webhook-test/...`, which only answers while the editor is listening."""
        route = str(path or "").strip("/")
        if not route:
            raise ProviderFailure("n8n webhook needs a path", provider="n8n")
        prefix = "webhook-test" if test else "webhook"
        url = f"/{prefix}/{route}"
        with self._client() as client:
            try:
                response = client.post(url, json=payload or {}, timeout=timeout or self.timeout)
            except httpx.HTTPError as exc:
                raise ProviderFailure(f"n8n webhook unreachable: {route}", error=str(exc)) from exc
        try:
            body = response.json() if response.content else {}
        except ValueError:
            body = {"text": response.text[:2000]}
        if response.status_code >= 400:
            raise ProviderFailure(
                f"n8n webhook {route} failed", status=response.status_code, body=body
            )
        return {
            "status": response.status_code,
            "webhook": f"/{prefix}/{route}",
            "responded": bool(body),
            "result": body,
        }


def make(base_url: str = DEFAULT_BASE, api_key: str = "", transport=None, timeout: float = 60.0):
    return N8NClient(base_url=base_url, api_key=api_key, transport=transport, timeout=timeout)
