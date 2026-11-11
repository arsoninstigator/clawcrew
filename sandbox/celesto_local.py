"""celesto, self-hosted: the OSS runtime on this machine.

the hosted api is not the point. celesto's repository ships the runtime that boots
local microVMs (`pip install celesto`, `celesto setup`, `celesto doctor`), so this
adapter drives *that* through the CLI, which is the interface celesto documents for
automation:

    celesto computer create --name NAME --json
    celesto computer exec   NAME --json --start -- sh -lc '<command>'
    celesto computer start|stop|pause|resume|delete|info|list NAME --json

every structured response is an envelope `{ok, command, exit_code, data, error}`.
a named computer persists between tasks, which is the whole point of the
`persistent` lifecycle: the writer keeps the brief it wrote last week, packages stay
installed, a browser profile survives.

`--json` everywhere, and `CELESTO_NO_TELEMETRY=1` unless the operator chose
otherwise, because a worker should not phone home on every exec.

not a boundary you should trust blindly: a local microVM is a real boundary only
when the host has working virtualization (`celesto doctor --strict`). this adapter
refuses to run when the runtime is missing rather than falling back to the host
shell.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import time
from typing import Any, Callable, Sequence

from core.errors import ProviderFailure, ProviderNotConfigured
from sandbox.base import BaseProvider, ExecResult, Handle

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
READY = {"running", "ready", "started", "active", "up"}


def slug(key: str) -> str:
    """a celesto computer name: stable per agent, safe for a shell and a file path."""
    cleaned = _UNSAFE.sub("-", str(key or "").strip()).strip("-.")
    return f"cc-{cleaned or 'computer'}"[:63]


class CelestoLocalProvider(BaseProvider):
    name = "celesto-local"
    kind = "persistent"
    isolated = True

    def __init__(
        self,
        binary: str = "celesto",
        runner: Callable[[Sequence[str], int], tuple[int, str, str]] | None = None,
        boot_timeout: int = 180,
        telemetry: bool = False,
    ):
        self.binary = binary or "celesto"
        self.boot_timeout = max(10, int(boot_timeout))
        self.telemetry = telemetry
        self._runner = runner

    # ---- cli plumbing -------------------------------------------------
    def configured(self) -> bool:
        if self._runner is not None:
            return True
        return bool(shutil.which(self.binary))

    def can_exec(self) -> bool:
        return self.configured()

    def _require(self) -> None:
        if not self.configured():
            raise ProviderNotConfigured(
                "the celesto OSS runtime is not installed on this machine",
                provider="celesto-local",
                hint="pip install celesto && celesto setup && celesto doctor",
            )

    def _run(self, args: Sequence[str], timeout: int | None = None) -> tuple[int, str, str]:
        self._require()
        argv = [str(a) for a in args]
        limit = int(timeout or self.boot_timeout)
        if self._runner is not None:
            return self._runner(argv, limit)
        env = dict(os.environ)
        if not self.telemetry:
            env.setdefault("CELESTO_NO_TELEMETRY", "1")
        try:
            proc = subprocess.run(
                [self.binary, *argv],
                capture_output=True,
                text=True,
                timeout=limit,
                env=env,
            )
        except FileNotFoundError as exc:  # binary vanished between checks
            raise ProviderNotConfigured(
                "the celesto CLI disappeared while running", provider="celesto-local"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderFailure(
                f"celesto {argv[0]} timed out after {limit}s", command=" ".join(argv)
            ) from exc
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    def _envelope(self, args: Sequence[str], what: str, timeout: int | None = None,
                  tolerate: bool = False) -> dict:
        code, out, err = self._run(args, timeout)
        envelope = _as_json(out)
        if envelope is None:
            raise ProviderFailure(
                f"{what}: celesto did not return json",
                command=" ".join(args),
                exit_code=code,
                stderr=err[-2000:],
            )
        failed = envelope.get("ok") is False or int(envelope.get("exit_code", code) or 0) != 0
        if failed:
            message = _error_text(envelope, err, code)
            if tolerate and _is_missing(message):
                return {**envelope, "_missing": True}
            raise ProviderFailure(
                f"{what} failed",
                command=" ".join(args),
                exit_code=int(envelope.get("exit_code", code) or 0),
                error=message,
            )
        return envelope

    # ---- lifecycle ----------------------------------------------------
    def ensure(self, key: str, spec: dict) -> Handle:
        self._require()
        name = slug(key)
        spec = spec or {}
        args = ["computer", "create", "--name", name, "--json"]
        if spec.get("desktop"):
            args.insert(2, "--desktop")
        for flag, field in (("--backend", "backend"), ("--boot-timeout", "boot_timeout")):
            if spec.get(field):
                args += [flag, str(spec[field])]
        created = True
        try:
            self._envelope(args, f"creating computer {name}", timeout=self.boot_timeout)
        except ProviderFailure as exc:
            text = str(exc.details.get("error") or exc)
            if not _already_exists(text):
                raise
            created = False
            self.start(Handle(self.name, "persistent", name))
        return Handle(
            provider=self.name,
            kind="persistent",
            ref=name,
            meta={"name": name, "created": created, "cli": self.binary},
        )

    def probe(self, handle: Handle) -> dict:
        envelope = self._envelope(
            ["computer", "info", handle.ref, "--json"], f"reading {handle.ref}", tolerate=True
        )
        data = envelope.get("data")
        return data if isinstance(data, dict) else {"raw": data}

    def is_ready(self, handle: Handle) -> bool:
        try:
            status = str(self.probe(handle).get("status", "")).lower()
        except ProviderFailure:
            return False
        return status in READY

    def start(self, handle: Handle) -> dict:
        return self._envelope(
            ["computer", "start", handle.ref, "--json"], f"starting {handle.ref}"
        ).get("data") or {}

    def stop(self, handle: Handle) -> dict:
        return self._envelope(
            ["computer", "stop", handle.ref, "--json"], f"stopping {handle.ref}"
        ).get("data") or {}

    def pause(self, handle: Handle, memory: bool = True) -> dict:
        return self._envelope(
            ["computer", "pause", handle.ref, "--json"], f"pausing {handle.ref}"
        ).get("data") or {}

    def resume(self, handle: Handle) -> dict:
        return self._envelope(
            ["computer", "resume", handle.ref, "--json"], f"resuming {handle.ref}"
        ).get("data") or {}

    def connect(self, handle: Handle) -> Handle:
        self.start(handle)
        return handle

    def destroy(self, handle: Handle) -> None:
        envelope = self._envelope(
            ["computer", "delete", handle.ref, "--json"], f"deleting {handle.ref}", tolerate=True
        )
        if envelope.get("_missing"):
            return None
        return None

    def list_computers(self) -> list[dict]:
        data = self._envelope(["computer", "list", "--json"], "listing computers").get("data")
        rows = data.get("computers") if isinstance(data, dict) else data
        return [row for row in (rows or []) if isinstance(row, dict)]

    # ---- work ---------------------------------------------------------
    def exec(self, handle: Handle, command: str, timeout: int = 300) -> ExecResult:
        started = time.time()
        code, out, err = self._run(
            [
                "computer", "exec", handle.ref, "--json", "--start", "--",
                "sh", "-lc", str(command),
            ],
            timeout=max(30, int(timeout)),
        )
        envelope = _as_json(out)
        if envelope is None:
            return ExecResult(exit_code=code or 1, stdout=out, stderr=err,
                              duration_ms=_ms(started))
        data = envelope.get("data")
        data = data if isinstance(data, dict) else {}
        exit_code = data.get("exit_code")
        if exit_code is None:
            exit_code = envelope.get("exit_code", code)
        return ExecResult(
            exit_code=int(exit_code or 0),
            stdout=str(data.get("stdout") or data.get("output") or ""),
            stderr=str(data.get("stderr") or err or ""),
            duration_ms=int(data.get("duration_ms") or _ms(started)),
        )

    def put_file(self, handle: Handle, path: str, content: str) -> None:
        """no shared filesystem assumption: write through exec so it works whether
        the computer is a local microVM or a remote one."""
        target = str(path)
        payload = base64.b64encode(str(content).encode()).decode()
        command = (
            f"mkdir -p \"$(dirname {_q(target)})\" && "
            f"printf %s {_q(payload)} | base64 -d > {_q(target)}"
        )
        result = self.exec(handle, command, timeout=120)
        if not result.ok:
            raise ProviderFailure(
                f"could not write {target} into {handle.ref}", stderr=result.stderr[-1000:]
            )

    def get_file(self, handle: Handle, path: str) -> str:
        result = self.exec(handle, f"base64 < {_q(str(path))}", timeout=120)
        if not result.ok:
            raise ProviderFailure(
                f"could not read {path} from {handle.ref}", stderr=result.stderr[-1000:]
            )
        try:
            return base64.b64decode(result.stdout.strip().encode()).decode()
        except Exception as exc:  # noqa: BLE001 - a corrupt read is a provider problem
            raise ProviderFailure(f"{path} did not decode as base64", error=str(exc)) from exc


def _q(value: str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def _ms(started: float) -> int:
    return int((time.time() - started) * 1000)


def _as_json(text: str) -> dict | None:
    """the envelope is the last json object on stdout; celesto prints human text
    before it in some setups, so take the last object that parses."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = __import__("json").loads(text)
        return parsed if isinstance(parsed, dict) else None
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = __import__("json").loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _error_text(envelope: dict, stderr: str, code: int) -> str:
    error = envelope.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("detail") or error)
    return str(error or stderr or f"exit {code}")


def _already_exists(text: str) -> bool:
    low = str(text or "").lower()
    return "already exists" in low or "already running" in low or "exists" in low


def _is_missing(text: str) -> bool:
    low = str(text or "").lower()
    return "not found" in low or "no computer" in low or "does not exist" in low


def make(binary: str = "celesto", boot_timeout: int = 180, runner=None, telemetry: bool = False):
    return CelestoLocalProvider(binary, runner=runner, boot_timeout=boot_timeout, telemetry=telemetry)
