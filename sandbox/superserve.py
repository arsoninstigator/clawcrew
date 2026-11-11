"""superserve adapter: firecracker sandboxes with desktop and preview ports.

superserve's repo (`superserve-ai/superserve`, apache-2.0) is the sdk, the console and
the docs. the firecracker runtime itself is their cloud, so unlike celesto and e2b there
is no runtime here to adapt: what is usable is the python sdk, and that is what this
file drives. it is an **opt in provider**, never the only path.

    pip install superserve
    SUPERSERVE_API_KEY=ss_live_... clawcrew get persistent --target superserve --apply

the sdk is the contract, so nothing here guesses an endpoint. every call is a documented
one from their readme: `Sandbox.create`, `commands.run`, `files.write`, `files.read_text`,
`kill`, `pause`, `get_signed_preview_url`. where their sdk offers `connect`, a handle
from another process can be reattached; where it does not, this says so instead of
pretending the sandbox is still reachable.
"""

from __future__ import annotations

import time
from typing import Any

from sandbox.base import BaseProvider, ExecResult, Handle
from core.errors import ProviderFailure, ProviderNotConfigured

DEFAULT_BASE = "https://api.superserve.ai"


def sdk() -> Any | None:
    """the superserve module, or None. imported lazily so the core has no dependency."""
    try:
        import superserve as module
    except ImportError:
        return None
    return module


class SuperserveProvider(BaseProvider):
    name = "superserve"
    kind = "ephemeral"  # their sandboxes pause and snapshot, so they can persist too

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE,
        template: str = "",
        module: Any | None = None,
    ):
        self.api_key = api_key or ""
        self.base_url = base_url or DEFAULT_BASE
        self.template = template
        self._module = module
        self._live: dict[str, Any] = {}

    @property
    def module(self):
        if self._module is None:
            self._module = sdk()
        return self._module

    def configured(self) -> bool:
        return bool(self.api_key) and self.module is not None

    def why_not(self) -> str:
        if not self.api_key:
            return "set SUPERSERVE_API_KEY"
        if self.module is None:
            return "pip install superserve"
        return ""

    def _require(self) -> None:
        if self.module is None:
            raise ProviderNotConfigured(
                "superserve sdk is not installed; pip install superserve", provider=self.name)
        if not self.api_key:
            raise ProviderNotConfigured(
                "superserve is not configured; set SUPERSERVE_API_KEY", provider=self.name)

    # ---- lifecycle -----------------------------------------------------
    def ensure(self, key: str, spec: dict) -> Handle:
        self._require()
        spec = spec or {}
        name = str(spec.get("name") or key)[:60]
        kwargs: dict[str, Any] = {"name": name, "api_key": self.api_key,
                                  "base_url": self.base_url}
        template = spec.get("template") or spec.get("from_template") or self.template
        if template:
            kwargs["from_template"] = template
        if spec.get("preview_access"):
            kwargs["preview_access"] = spec["preview_access"]
        try:
            sandbox = self.module.Sandbox.create(**kwargs)
        except Exception as exc:  # their error ladder, kept intact in the message
            raise ProviderFailure(f"superserve could not create '{name}': {exc}") from exc
        ref = str(getattr(sandbox, "id", "") or getattr(sandbox, "sandbox_id", ""))
        if not ref:
            raise ProviderFailure("superserve did not return a sandbox id")
        self._live[ref] = sandbox
        return Handle(provider=self.name, kind="ephemeral", ref=ref,
                      meta={"key": key, "name": name, "template": template})

    def sandbox(self, handle: Handle):
        live = self._live.get(handle.ref)
        if live is not None:
            return live
        connect = getattr(getattr(self.module, "Sandbox", None), "connect", None)
        if callable(connect):
            live = connect(handle.ref, api_key=self.api_key, base_url=self.base_url)
            self._live[handle.ref] = live
            return live
        raise ProviderFailure(
            f"sandbox {handle.ref} was created by another process and this sdk version "
            "has no connect(); re-create it with `clawcrew get`, the handle is stale")

    def is_ready(self, handle: Handle) -> bool:
        return bool(handle.ref)

    def pause(self, handle: Handle) -> dict:
        self._require()
        self.sandbox(handle).pause()
        return {"ref": handle.ref, "state": "paused"}

    def preview_url(self, handle: Handle, port: int, expires_in: int = 300) -> dict:
        """a signed preview link, so a human can look at what the agent built."""
        self._require()
        sandbox = self.sandbox(handle)
        return {"port": port,
                "url": sandbox.get_signed_preview_url(port, expires_in_seconds=expires_in)}

    def destroy(self, handle: Handle) -> None:
        live = self._live.pop(handle.ref, None)
        if live is not None:
            live.kill()

    # ---- inside the sandbox -------------------------------------------
    def exec(self, handle: Handle, command: str, timeout: int = 300) -> ExecResult:
        self._require()
        started = time.monotonic()
        try:
            result = self.sandbox(handle).commands.run(command, timeout_seconds=timeout)
        except Exception as exc:
            raise ProviderFailure(f"superserve exec failed: {exc}") from exc
        return ExecResult(
            exit_code=int(getattr(result, "exit_code", 0) or 0),
            stdout=str(getattr(result, "stdout", "") or ""),
            stderr=str(getattr(result, "stderr", "") or ""),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def put_file(self, handle: Handle, path: str, content: str) -> None:
        self._require()
        self.sandbox(handle).files.write(path, content.encode())

    def get_file(self, handle: Handle, path: str) -> str:
        self._require()
        return str(self.sandbox(handle).files.read_text(path))


def make(
    api_key: str,
    base_url: str = DEFAULT_BASE,
    template: str = "",
    module: Any | None = None,
) -> SuperserveProvider:
    return SuperserveProvider(api_key, base_url, template, module=module)
