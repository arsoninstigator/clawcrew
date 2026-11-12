"""sandbox provider contract.

two lifecycles matter for crittertown:

* **persistent computers** - one per agent, kept alive between tasks. the agent's
  files, browser profile and installed tooling survive. backed by persistent.
* **ephemeral sandboxes** - one per task (or per risky step), thrown away after.
  backed by ephemeral.

both satisfy `SandboxProvider`, so orchestration code never branches on vendor.
`spec` is a free-form dict (`image`, `template`, `ttl_seconds`, ...) that the adapter
translates.

`env` is the one argument that is not free-form. it carries credentials, so it crosses
this interface explicitly rather than riding along in `spec` where anything could add
to it:

* a provider whose create request has an env field (`delivers_env = True`, the default)
  is handed the values at creation, and they never touch a row, a response or a log
* a provider whose create request has no such field (`delivers_env = False`: persistent
  computers, whose create schema has no env, and hosted, whose sdk surface is theirs)
  is handed nothing. its harness pulls the same values from the platform over the
  authenticated channel at boot, so a secret never sits in an exec command line or on
  the machine's disk
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from core.errors import ProviderFailure, ProviderNotConfigured, ValidationError


# a name a provider may be handed. strict on purpose: `=` or a newline in a name or a
# value is how one variable becomes two, and this argument is where credentials arrive.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def clean_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """validate the env a sandbox is handed, and never trust the caller's dict."""
    if not env:
        return {}
    out: dict[str, str] = {}
    for name, value in dict(env).items():
        if not isinstance(name, str) or not _ENV_NAME.match(name):
            raise ValidationError(f"invalid env var name: {name!r}")
        if not isinstance(value, str):
            raise ValidationError(f"env var {name} must be a string")
        if any(ch in value for ch in ("\n", "\r", "\0")):
            raise ValidationError(f"env var {name} contains a newline or NUL")
        out[name] = value
    return out


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

    def ensure(self, key: str, spec: dict,
               env: Mapping[str, str] | None = None) -> Handle: ...

    def exec(self, handle: Handle, command: str, timeout: int = 300) -> ExecResult: ...

    def put_file(self, handle: Handle, path: str, content: str) -> None: ...

    def get_file(self, handle: Handle, path: str) -> str: ...

    def destroy(self, handle: Handle) -> None: ...


class BaseProvider:
    name = "base"
    kind = "ephemeral"
    # does this provider's create request take env? if not, the platform delivers the
    # same values to the harness at boot instead. see the module docstring.
    delivers_env = True

    def configured(self) -> bool:  # overridden
        return False

    def _require(self) -> None:
        if not self.configured():
            raise ProviderNotConfigured(
                f"{self.name} is not configured; set its api key or enable the local fallback",
                provider=self.name,
            )

    def ensure(self, key: str, spec: dict,
               env: Mapping[str, str] | None = None) -> Handle:  # pragma: no cover
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
