"""ephemeral adapter: ephemeral sandboxes.

the lifecycle runs on ephemeral's **generated** openapi client, vendored in
`clawcrew/vendor/ephemeral_api` (MIT, source in ephemeral-dev/Ephemeral, `spec/openapi.yml`):

    POST   /sandboxes                      NewSandbox -> Sandbox
    GET    /sandboxes/{id}                 SandboxDetail
    POST   /sandboxes/{id}/pause | /resume | /connect
    DELETE /sandboxes/{id}

important correction to the obvious implementation: **commands do not go through the
sandbox REST api**. exec runs over envd, the daemon inside the sandbox, on a
connectrpc contract that ships in ephemeral's own sdk. that hop is not reimplemented here;
this adapter uses the sdk when it is importable and refuses with a precise error when
it is not, instead of inventing a route that does not exist.

    pip install "clawcrew[ephemeral]"     # then EPHEMERAL_API_KEY=... works end to end

persistence here is deliberately short: an ephemeral sandbox exists for one task and
is killed in a `finally`. `pause` exists for when a task needs to survive a worker
restart, and resume is explicit.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from core.errors import ProviderFailure, ProviderNotConfigured
from vendor.ephemeral_cloud_api.api.sandboxes import (
    delete_sandboxes_sandbox_id as delete_op,
    get_sandboxes_sandbox_id as get_op,
    post_sandboxes as create_op,
    post_sandboxes_sandbox_id_connect as connect_op,
    post_sandboxes_sandbox_id_pause as pause_op,
    post_sandboxes_sandbox_id_resume as resume_op,
)
from vendor.ephemeral_cloud_api.models.connect_sandbox import ConnectSandbox
from vendor.ephemeral_cloud_api.models.new_sandbox import NewSandbox
from vendor.ephemeral_cloud_api.models.resumed_sandbox import ResumedSandbox
from vendor.ephemeral_cloud_api.models.sandbox import Sandbox
from vendor.ephemeral_cloud_api.models.sandbox_detail import SandboxDetail
from vendor.ephemeral_cloud_api.models.sandbox_pause_request import SandboxPauseRequest
from sandbox.base import BaseProvider, ExecResult, Handle
from sandbox.transport import DEFAULT_EPHEMERAL_BASE, call, ephemeral_client, field, guard, unwrap

READY_STATES = {"running", "ready", "active", "started"}


def _sdk():
    """return the sandbox class from the upstream python sdk, or None.

    the envd hop is a connectrpc contract that only the sdk implements, so the sdk is
    used rather than re-derived. its module name is the one place outside
    `clawcrew/thirdparty.md` where an upstream project is named: a python import
    cannot be aliased, and `EPHEMERAL_EXEC_MODULE` exists for a fork that renamed it.
    """
    for module in (os.environ.get("EPHEMERAL_EXEC_MODULE") or "e2b", "e2b_code_interpreter"):
        try:
            mod = __import__(module, fromlist=["Sandbox"])
        except ImportError:
            continue
        sandbox = getattr(mod, "Sandbox", None)
        if sandbox is not None:
            return sandbox
    return None


def _ms(started: float) -> int:
    return int((time.time() - started) * 1000)


class EphemeralProvider(BaseProvider):
    name = "ephemeral"
    kind = "ephemeral"
    isolated = True

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_EPHEMERAL_BASE,
        template: str = "base",
        transport: httpx.BaseTransport | None = None,
    ):
        self.api_key = api_key or ""
        self.base_url = base_url or DEFAULT_EPHEMERAL_BASE
        self.template = template
        self._transport = transport
        self._api = None

    @property
    def api(self):
        if self._api is None:
            self._api = ephemeral_client(self.api_key, self.base_url, transport=self._transport)
        return self._api

    def configured(self) -> bool:
        return bool(self.api_key)

    def can_exec(self) -> bool:
        """exec needs the sdk hop through envd, not just an api key."""
        return self.configured() and _sdk() is not None

    # ---- translation --------------------------------------------------
    def build_spec(self, key: str, spec: dict) -> dict:
        spec = spec or {}
        body: dict[str, Any] = {
            "template_id": spec.get("template")
            or spec.get("template_id")
            or spec.get("templateID")
            or self.template,
            "timeout": int(spec.get("ttl_seconds") or spec.get("timeout") or 900),
            "metadata": {"clawcrew_key": key, **(spec.get("metadata") or {})},
        }
        env = spec.get("env") or spec.get("envVars") or spec.get("env_vars")
        if env:
            body["env_vars"] = env
        if spec.get("allow_internet_access") is not None:
            body["allow_internet_access"] = bool(spec["allow_internet_access"])
        if spec.get("network"):
            body["network"] = spec["network"]
        if spec.get("auto_pause") is not None:
            body["auto_pause"] = bool(spec["auto_pause"])
        return body

    def handle_from(self, sandbox: Any, key: str) -> Handle:
        token = field(sandbox, "envd_access_token", "")
        return Handle(
            provider=self.name,
            kind="ephemeral",
            ref=str(field(sandbox, "sandbox_id", "")),
            meta={
                "key": key,
                "templateID": field(sandbox, "template_id", self.template),
                "envd_version": field(sandbox, "envd_version", ""),
                "domain": field(sandbox, "domain", ""),
                # the envd token is a credential: record that we have one, never the value
                "has_envd_token": bool(token),
                "client_id": field(sandbox, "client_id", ""),
            },
        )

    # ---- lifecycle ----------------------------------------------------
    def ensure(self, key: str, spec: dict) -> Handle:
        self._require()
        body = NewSandbox(**self.build_spec(key, spec))
        response = call(
            create_op.sync_detailed,
            client=self.api,
            body=body,
            what=f"creating sandbox for '{key}'",
        )
        sandbox = unwrap(response, Sandbox, f"creating sandbox for '{key}'")
        handle = self.handle_from(sandbox, key)
        if not handle.ref:
            raise ProviderFailure("ephemeral did not return a sandbox id")
        handle.meta["expires_in"] = int(field(body, "timeout", 900))
        return handle

    def probe(self, handle: Handle) -> dict:
        self._require()
        response = call(
            get_op.sync_detailed, handle.ref, client=self.api, what=f"reading {handle.ref}"
        )
        return unwrap(response, SandboxDetail, f"reading {handle.ref}").to_dict()

    def is_ready(self, handle: Handle) -> bool:
        try:
            state = str(self.probe(handle).get("state", "")).lower()
        except ProviderFailure:
            return False
        return state in READY_STATES

    def pause(self, handle: Handle, memory: bool = True) -> dict:
        self._require()
        response = call(
            pause_op.sync_detailed,
            handle.ref,
            client=self.api,
            body=SandboxPauseRequest(memory=memory),
            what=f"pausing {handle.ref}",
        )
        return _as_dict(response)

    def resume(self, handle: Handle, timeout: int = 900) -> Handle:
        self._require()
        response = call(
            resume_op.sync_detailed,
            handle.ref,
            client=self.api,
            body=ResumedSandbox(timeout=int(timeout)),
            what=f"resuming {handle.ref}",
        )
        sandbox = unwrap(response, Sandbox, f"resuming {handle.ref}")
        fresh = self.handle_from(sandbox, handle.meta.get("key", ""))
        fresh.meta["resumed"] = True
        return fresh

    def connect(self, handle: Handle, timeout: int = 900) -> Handle:
        self._require()
        response = call(
            connect_op.sync_detailed,
            handle.ref,
            client=self.api,
            body=ConnectSandbox(timeout=int(timeout)),
            what=f"connecting to {handle.ref}",
        )
        sandbox = unwrap(response, Sandbox, f"connecting to {handle.ref}")
        fresh = self.handle_from(sandbox, handle.meta.get("key", ""))
        fresh.meta["connected"] = True
        return fresh

    def destroy(self, handle: Handle) -> None:
        """killing a sandbox that already expired is not an error."""
        if not self.configured():
            return None
        try:
            response = call(
                delete_op.sync_detailed, handle.ref, client=self.api, what=f"killing {handle.ref}"
            )
            guard(response, f"killing {handle.ref}")
        except ProviderFailure as exc:
            if exc.details.get("status") != 404:
                raise
        return None

    # ---- work ---------------------------------------------------------
    def exec(self, handle: Handle, command: str, timeout: int = 300) -> ExecResult:
        self._require()
        sandbox_class = _sdk()
        if sandbox_class is None:
            raise ProviderNotConfigured(
                "exec in an ephemeral sandbox needs the ephemeral sdk (the command hop goes through envd)",
                provider="ephemeral",
                hint='pip install "clawcrew[ephemeral]"',
            )
        started = time.time()
        try:
            sandbox = sandbox_class.connect(handle.ref, api_key=self.api_key)
        except Exception as exc:  # noqa: BLE001 - sdk raises vendor types
            raise ProviderFailure(f"ephemeral could not connect to {handle.ref}: {exc}") from exc
        try:
            result = sandbox.commands.run(command, timeout=max(1, int(timeout)))
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "timed out" in text.lower() or "timeout" in text.lower():
                return ExecResult(exit_code=124, stderr=text, duration_ms=_ms(started))
            return ExecResult(exit_code=1, stderr=text, duration_ms=_ms(started))
        return ExecResult(
            exit_code=int(getattr(result, "exit_code", 0) or 0),
            stdout=str(getattr(result, "stdout", "") or ""),
            stderr=str(getattr(result, "stderr", "") or ""),
            duration_ms=int(getattr(result, "duration_ms", 0) or _ms(started)),
        )

    def put_file(self, handle: Handle, path: str, content: str) -> None:
        self._require()
        sandbox_class = _sdk()
        if sandbox_class is None:
            raise ProviderNotConfigured(
                "writing files into an ephemeral sandbox needs the ephemeral sdk",
                provider="ephemeral",
                hint='pip install "clawcrew[ephemeral]"',
            )
        sandbox = sandbox_class.connect(handle.ref, api_key=self.api_key)
        sandbox.files.write(path, content)

    def get_file(self, handle: Handle, path: str) -> str:
        self._require()
        sandbox_class = _sdk()
        if sandbox_class is None:
            raise ProviderNotConfigured(
                "reading files from an ephemeral sandbox needs the ephemeral sdk",
                provider="ephemeral",
                hint='pip install "clawcrew[ephemeral]"',
            )
        sandbox = sandbox_class.connect(handle.ref, api_key=self.api_key)
        return sandbox.files.read(path)


def _as_dict(response) -> dict:
    parsed = getattr(response, "parsed", None)
    if hasattr(parsed, "to_dict"):
        return parsed.to_dict()
    if isinstance(parsed, dict):
        return parsed
    return {"status_code": int(getattr(response, "status_code", 0) or 0)}


def make(
    api_key: str,
    base_url: str = DEFAULT_EPHEMERAL_BASE,
    template: str = "base",
    transport: httpx.BaseTransport | None = None,
) -> EphemeralProvider:
    return EphemeralProvider(api_key, base_url, template, transport=transport)
