"""langflow, self-hosted.

langflow is an oss flow builder (mit). nothing of it is copied here: a workflow node
posts to the flow api on **your own** instance, `POST /api/v1/run/{flow_id}` with an
`x-api-key` header, which is the shape langflow publishes for its own ui.

`base_url` is the instance root. `/api/v1` is added when missing, because people paste
both `http://localhost:7860` and `http://localhost:7860/api/v1`.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from core.errors import ProviderFailure, ProviderNotConfigured

DEFAULT_BASE = "http://localhost:7860"


def _root(base_url: str) -> str:
    root = (base_url or DEFAULT_BASE).strip().rstrip("/")
    for suffix in ("/api/v1", "/api"):
        if root.endswith(suffix):
            root = root[: -len(suffix)]
    return root + "/api/v1"


@dataclass
class LangflowClient:
    base_url: str = DEFAULT_BASE
    api_key: str = ""
    timeout: float = 120.0
    transport: httpx.BaseTransport | None = None

    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def status(self) -> dict:
        return {
            "kind": "langflow",
            "self_hosted": True,
            "base_url": _root(self.base_url),
            "configured": self.configured(),
            "needs": "CLAWCREW_LANGFLOW_BASE_URL and CLAWCREW_LANGFLOW_API_KEY (a langflow api key)",
        }

    def _client(self) -> httpx.Client:
        if not self.configured():
            raise ProviderNotConfigured(
                "no langflow api key",
                provider="langflow",
                hint="open your langflow instance, settings > langflow api keys, create one",
            )
        return httpx.Client(
            base_url=_root(self.base_url),
            headers={"x-api-key": self.api_key, "Accept": "application/json"},
            timeout=self.timeout,
            transport=self.transport,
        )

    def run_flow(self, flow_id: str, input_value: str = "",
                 tweaks: dict | None = None, session_id: str = "") -> dict:
        """run one flow and come back with its text, not its envelope."""
        if not str(flow_id or "").strip():
            raise ProviderFailure("langflow run needs a flow id", provider="langflow")
        payload = {
            "input_value": input_value,
            "output_type": "chat",
            "input_type": "chat",
            "tweaks": tweaks or {},
        }
        if session_id:
            payload["session_id"] = session_id
        with self._client() as client:
            response = client.post(f"/run/{flow_id}", json=payload)
            if response.status_code >= 400:
                raise ProviderFailure(
                    f"langflow returned {response.status_code}",
                    provider="langflow",
                    hint=response.text[:300],
                )
            return parse_run(response.json())

    def health(self) -> dict:
        try:
            with self._client() as client:
                response = client.get("/version")
            return {"ok": response.status_code < 400, "status": response.status_code}
        except Exception as exc:  # noqa: BLE001 - a health probe reports, it does not raise
            return {"ok": False, "error": str(exc)}


def parse_run(data: dict) -> dict:
    """pull the text out of langflow's nested envelope, without guessing too hard."""
    if not isinstance(data, dict):
        return {"text": str(data), "raw": data}
    text = ""
    outputs = data.get("outputs")
    if isinstance(outputs, list) and outputs:
        first = outputs[0] or {}
        for key in ("outputs", "results"):
            branch = first.get(key)
            if isinstance(branch, list) and branch:
                item = branch[0] or {}
                text = (item.get("results") or {}).get("message", {}).get("text") or item.get("text") or text
            elif isinstance(branch, dict):
                text = branch.get("message", {}).get("text") or branch.get("text") or text
    if not text:
        text = data.get("text") or data.get("output") or ""
    return {"text": text, "session_id": data.get("session_id", ""),
            "flow_id": data.get("flow_id", ""), "raw": data}


def make(base_url: str = "", api_key: str = "",
         transport: httpx.BaseTransport | None = None) -> LangflowClient:
    return LangflowClient(base_url=base_url or DEFAULT_BASE, api_key=api_key, transport=transport)
