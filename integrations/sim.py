"""sim, self-hosted.

sim (simstudioai/sim, apache-2.0) is an oss workflow builder. like langflow and dify, no
source is copied: a workflow node calls `POST /api/workflows/{id}/execute` on your own
instance with an `x-api-key`, and gets an execution record back.

the response shapes of the workflow runner have moved between releases, so this client
returns what it got under `raw` and lifts `output`/`executionId` when they are there
instead of pretending to a fixed schema.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from core.errors import ProviderFailure, ProviderNotConfigured

DEFAULT_BASE = "http://localhost:3000"


def _root(base_url: str) -> str:
    return (base_url or DEFAULT_BASE).strip().rstrip("/")


@dataclass
class SimClient:
    base_url: str = DEFAULT_BASE
    api_key: str = ""
    timeout: float = 120.0
    transport: httpx.BaseTransport | None = None

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def status(self) -> dict:
        return {
            "kind": "sim",
            "self_hosted": True,
            "base_url": _root(self.base_url),
            "configured": self.configured(),
            "needs": "CLAWCREW_SIM_BASE_URL and CLAWCREW_SIM_API_KEY (a sim api key)",
        }

    def _client(self) -> httpx.Client:
        if not self.configured():
            raise ProviderNotConfigured(
                "no sim api key",
                provider="sim",
                hint="start sim, then settings > api keys, create one",
            )
        return httpx.Client(
            base_url=_root(self.base_url),
            headers={"x-api-key": self.api_key, "Accept": "application/json"},
            timeout=self.timeout,
            transport=self.transport,
        )

    def execute(self, workflow_id: str, inputs: dict | None = None,
                sync: bool = True) -> dict:
        if not str(workflow_id or "").strip():
            raise ProviderFailure("sim execute needs a workflow id", provider="sim")
        payload = {"input": inputs or {}}
        if not sync:
            payload["async"] = True
        with self._client() as client:
            response = client.post(f"/api/workflows/{workflow_id}/execute", json=payload)
            if response.status_code >= 400:
                raise ProviderFailure(
                    f"sim returned {response.status_code}",
                    provider="sim",
                    hint=response.text[:300],
                )
            return parse_execution(response.json())

    def health(self) -> dict:
        try:
            with self._client() as client:
                response = client.get("/api/health")
            return {"ok": response.status_code < 400, "status": response.status_code}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}


def parse_execution(data: dict) -> dict:
    if not isinstance(data, dict):
        return {"ok": True, "output": data, "raw": data}
    failed = data.get("success") is False or str(data.get("status", "")).lower() in {"failed", "error"}
    return {
        "ok": not failed,
        "execution_id": data.get("executionId") or data.get("execution_id") or "",
        "output": data.get("output", data.get("result")),
        "error": "" if not failed else str(data.get("error") or data.get("output") or "sim reported failure"),
        "raw": data,
    }


def make(base_url: str = "", api_key: str = "",
         transport: httpx.BaseTransport | None = None) -> SimClient:
    return SimClient(base_url=base_url or DEFAULT_BASE, api_key=api_key, transport=transport)
