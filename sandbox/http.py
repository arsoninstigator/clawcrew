"""tiny json-over-http helper built on urllib so the backend has no third party
http dependency."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from core.errors import ProviderFailure


def request_json(
    method: str,
    url: str,
    *,
    payload: dict | None = None,
    headers: dict | None = None,
    timeout: int = 60,
) -> Any:
    data = None
    hdrs = {"accept": "application/json", **{k: v for k, v in (headers or {}).items() if v}}
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs["content-type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            if not body:
                return {}
            try:
                return json.loads(body)
            except ValueError:
                return {"raw": body}
    except urllib.error.HTTPError as exc:  # pragma: no cover - network
        detail = exc.read().decode("utf-8", "replace")[:2000]
        raise ProviderFailure(
            f"{method} {url} failed with {exc.code}", status=exc.code, body=detail
        ) from exc
    except urllib.error.URLError as exc:  # pragma: no cover - network
        raise ProviderFailure(f"{method} {url} unreachable: {exc.reason}") from exc


def pick(raw: dict, *keys: str, default: Any = "") -> Any:
    """providers rename fields constantly; read the first key that exists."""
    for key in keys:
        if isinstance(raw, dict) and key in raw and raw[key] is not None:
            return raw[key]
    return default
