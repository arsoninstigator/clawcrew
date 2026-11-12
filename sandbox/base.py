"""sandbox provider contract.

two lifecycles matter for clawcrew:

* **persistent computers** - one per agent, kept alive between tasks. the agent's
  files, browser profile and installed tooling survive. backed by persistent.
* **ephemeral sandboxes** - one per task (or per risky step), thrown away after.
  backed by ephemeral.

both satisfy `SandboxProvider`, so orchestration code never branches on vendor.
`spec` is a free-form dict (`image`, `template`, `ttl_seconds`, `env`, ...) that
the adapter translates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from core.errors import ProviderFailure, ProviderNotConfigured, ValidationError


@dataclass
class ExecResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
        }


@dataclass
class Handle:
    """opaque pointer to a live sandbox. stored verbatim in `computers.handle`
    or `runs.sandbox_id`."""

    provider: str
    kind: str  # persistent | ephemeral
    ref: str
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"provider": self.provider, "kind": self.kind, "ref": self.ref, "meta": self.meta}

    @classmethod
    def from_dict(cls, raw: Any) -> "Handle":
        if isinstance(raw, Handle):
            return raw
        if not raw:
            raise ProviderFailure("empty sandbox handle")
        if isinstance(raw, str):
            return cls(provider="unknown", kind="unknown", ref=raw)
        return cls(
            provider=raw.get("provider", "unknown"),
            kind=raw.get("kind", "unknown"),
            ref=raw.get("ref", ""),
            meta=raw.get("meta", {}) or {},
        )


@runtime_checkable
class SandboxProvider(Protocol):
    name: str
    kind: str

    def configured(self) -> bool: ...

    def ensure(self, key: str, spec: dict) -> Handle: ...

    def exec(self, handle: Handle, command: str, timeout: int = 300) -> ExecResult: ...

    def put_file(self, handle: Handle, path: str, content: str) -> None: ...

    def get_file(self, handle: Handle, path: str) -> str: ...

    def destroy(self, handle: Handle) -> None: ...


class BaseProvider:
    name = "base"
    kind = "ephemeral"

    def configured(self) -> bool:  # overridden
        return False

    def _require(self) -> None:
        if not self.configured():
            raise ProviderNotConfigured(
                f"{self.name} is not configured; set its api key or enable the local fallback",
                provider=self.name,
            )

    def ensure(self, key: str, spec: dict) -> Handle:  # pragma: no cover - interface
        raise NotImplementedError

    def exec(self, handle: Handle, command: str, timeout: int = 300) -> ExecResult:
        raise NotImplementedError

    def put_file(self, handle: Handle, path: str, content: str) -> None:
        raise ProviderFailure(f"{self.name} does not support put_file")

    def get_file(self, handle: Handle, path: str) -> str:
        raise ProviderFailure(f"{self.name} does not support get_file")

    def destroy(self, handle: Handle) -> None:
        return None


def validate_spec(spec: Any) -> dict:
    if spec is None:
        return {}
    if not isinstance(spec, dict):
        raise ValidationError("sandbox spec must be an object")
    return spec
