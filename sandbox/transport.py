"""shared plumbing for the hosted providers.

both adapters talk through the openapi clients generated from each vendor's own
spec and vendored into `vendor/`, so the request paths, headers and body
shapes are theirs, not strings we typed. this module owns the three things the two
adapters would otherwise duplicate:

* building a client (base url normalisation, auth header, transport injection)
* turning a vendor response into either a model or a `ProviderFailure`
* reading attrs fields without caring whether the vendor sent them

transport injection exists so tests can drive the real generated code with an
`httpx.MockTransport` and assert on what actually goes over the wire.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from core.errors import ProviderFailure
from vendor.persistent_cloud_api.client import AuthenticatedClient as PersistentClient
from vendor.ephemeral_cloud_api.client import AuthenticatedClient as EphemeralClient

DEFAULT_PERSISTENT_BASE = "https://api.machines.internal"
DEFAULT_EPHEMERAL_BASE = "https://api.ephemeral.internal"


def api_root(base_url: str, default: str, version_suffix: str = "") -> str:
    """the generated clients carry the version segment in every route, so a
    `.../v1` base url would double it up."""
    url = (base_url or default).strip().rstrip("/")
    if version_suffix and url.endswith(version_suffix):
        url = url[: -len(version_suffix)].rstrip("/")
    return url or default


def _httpx_args(transport: httpx.BaseTransport | None) -> dict:
    return {"transport": transport} if transport is not None else {}


def persistent_client(
    api_key: str,
    base_url: str = DEFAULT_PERSISTENT_BASE,
    *,
    client_tag: str = "crittertown",
    timeout: float = 60.0,
    transport: httpx.BaseTransport | None = None,
) -> PersistentClient:
    return PersistentClient(
        base_url=api_root(base_url, DEFAULT_PERSISTENT_BASE, "/v1"),
        token=api_key,
        prefix="Bearer",
        timeout=httpx.Timeout(timeout),
        headers={"x-persistent-client": client_tag},
        raise_on_unexpected_status=True,
        httpx_args=_httpx_args(transport),
    )


def ephemeral_client(
    api_key: str,
    base_url: str = DEFAULT_EPHEMERAL_BASE,
    *,
    timeout: float = 60.0,
    transport: httpx.BaseTransport | None = None,
) -> EphemeralClient:
    # ephemeral authenticates with a bare X-API-KEY header, not a bearer token.
    return EphemeralClient(
        base_url=api_root(base_url, DEFAULT_EPHEMERAL_BASE),
        token=api_key,
        prefix="",
        auth_header_name="X-API-KEY",
        timeout=httpx.Timeout(timeout),
        raise_on_unexpected_status=True,
        httpx_args=_httpx_args(transport),
    )


def is_unset(value: Any) -> bool:
    """the two generated packages each ship their own UnsetType."""
    return type(value).__name__ in {"UnsetType", "Unset"}


def field(model: Any, name: str, default: Any = None) -> Any:
    value = getattr(model, name, default)
    if is_unset(value) or value is None:
        return default
    return value


def body_of(response: httpx.Response | Any) -> str:
    content = getattr(response, "content", b"") or b""
    if isinstance(content, bytes):
        text = content.decode("utf-8", "replace")
    else:
        text = str(content)
    return text[:2000]


def unwrap(response: Any, want: type | tuple[type, ...], what: str) -> Any:
    """`sync_detailed` gives back a Response with a parsed body. only the model we
    asked for counts as success; a documented 4xx comes back as a validation model
    and becomes a ProviderFailure carrying the body the vendor actually sent."""
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, want):
        return parsed
    status = int(getattr(response, "status_code", 0) or 0)
    detail = body_of(response)
    if isinstance(parsed, dict):
        detail = json.dumps(parsed, sort_keys=True)[:2000]
    raise ProviderFailure(
        f"{what} failed with {status or 'no status'}", status=status, body=detail
    )


def guard(response: Any, what: str) -> Any:
    """for endpoints whose success body carries nothing worth parsing: a documented
    4xx/5xx still comes back as a parsed error model, so the status is the check."""
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        raise ProviderFailure(f"{what} failed with {status}", status=status, body=body_of(response))
    parsed = getattr(response, "parsed", None)
    return parsed


def call(fn, *args, what: str, **kwargs) -> Any:
    """run a generated endpoint call, mapping vendor and network errors onto the
    crittertown ladder so orchestration never sees an httpx exception."""
    try:
        return fn(*args, **kwargs)
    except ProviderFailure:
        raise
    except httpx.TimeoutException as exc:
        raise ProviderFailure(f"{what} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ProviderFailure(f"{what} could not reach the provider: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - vendor UnexpectedStatus carries a status
        status = getattr(exc, "status_code", None)
        if status is not None:
            raise ProviderFailure(
                f"{what} failed with {int(status)}",
                status=int(status),
                body=body_of(exc),
            ) from exc
        if isinstance(exc, (ValueError, KeyError, TypeError)):
            # the generated parser rejects a body its own spec says should be valid
            raise ProviderFailure(
                f"{what}: the provider sent a body that does not match its published schema",
                body=str(exc)[:2000],
            ) from exc
        raise
