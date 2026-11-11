"""local shell provider.

used for development, tests, and single-tenant self-hosting with no cloud
sandbox. persistent computers get a stable directory under the data root;
ephemeral sandboxes get a temp dir that is removed on destroy.

this is not a security boundary. `exec` runs through the host shell, so a
deployment that lets untrusted agents run arbitrary commands must use celesto or
e2b instead. the api surfaces `provider.isolated = False` so callers can tell.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Any

from sandbox.base import BaseProvider, ExecResult, Handle

# a deliberately small allowlist of binaries for the local provider. anything
# else is refused, which keeps the dev provider from becoming a foot-gun.
ALLOWED_BINARIES = {
    "bash", "sh", "ls", "cat", "echo", "pwd", "mkdir", "cp", "mv", "rm", "touch",
    "python", "python3", "pip", "pip3", "node", "npm", "npx", "git", "grep",
    "sed", "awk", "find", "curl", "wget", "jq", "head", "tail", "wc", "sort",
    "uniq", "date", "env", "test",
}


class LocalProvider(BaseProvider):
    name = "local"
    kind = "both"
    isolated = False

    def __init__(self, root: str = ""):
        self.root = root or os.path.join(tempfile.gettempdir(), "clawcrew-local")
        os.makedirs(self.root, exist_ok=True)

    def configured(self) -> bool:
        return True

    def _dir(self, key: str, kind: str) -> str:
        safe = "".join(ch for ch in key if ch.isalnum() or ch in "-_") or "anon"
        path = os.path.join(self.root, kind, safe)
        os.makedirs(path, exist_ok=True)
        return path

    def ensure(self, key: str, spec: dict) -> Handle:
        spec = spec or {}
        kind = spec.get("kind", "ephemeral")
        if kind == "persistent":
            path = self._dir(key, "persistent")
        else:
            path = tempfile.mkdtemp(prefix="cc-eph-", dir=self._dir(key, "ephemeral"))
        return Handle(provider=self.name, kind=kind, ref=path, meta={"cwd": path})

    def _guard(self, command: str) -> None:
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return  # let the shell report the syntax error
        for token in tokens:
            head = token.split("/")[-1]
            if head in ALLOWED_BINARIES:
                return
        # no allowed binary in the command line; refuse rather than guess
        from core.errors import NotAllowed

        raise NotAllowed(
            "local provider only allows a small binary allowlist; use celesto or e2b for arbitrary workloads",
            command=command,
        )

    def exec(self, handle: Handle, command: str, timeout: int = 300) -> ExecResult:
        self._guard(command)
        started = time.time()
        try:
            proc = subprocess.run(
                ["/bin/bash", "-lc", command],
                cwd=handle.ref,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "CLAWCREW_SANDBOX": handle.ref},
            )
        except subprocess.TimeoutExpired:
            return ExecResult(
                exit_code=124,
                stdout="",
                stderr=f"timed out after {timeout}s",
                duration_ms=int((time.time() - started) * 1000),
            )
        return ExecResult(
            exit_code=proc.returncode,
            stdout=proc.stdout[-20000:],
            stderr=proc.stderr[-20000:],
            duration_ms=int((time.time() - started) * 1000),
        )

    def put_file(self, handle: Handle, path: str, content: str) -> None:
        target = os.path.join(handle.ref, path.lstrip("/"))
        os.makedirs(os.path.dirname(target) or handle.ref, exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(content)

    def get_file(self, handle: Handle, path: str) -> str:
        target = os.path.join(handle.ref, path.lstrip("/"))
        with open(target, "r", encoding="utf-8") as fh:
            return fh.read()

    def destroy(self, handle: Handle) -> None:
        if handle.kind != "ephemeral":
            return
        ephemeral_root = os.path.realpath(os.path.join(self.root, "ephemeral"))
        target = os.path.realpath(handle.ref)
        if os.path.commonpath([ephemeral_root, target]) == ephemeral_root and target != ephemeral_root:
            shutil.rmtree(target, ignore_errors=True)

    def snapshot(self, handle: Handle) -> dict:
        files = []
        for dirpath, _dirnames, filenames in os.walk(handle.ref):
            for name in filenames[:200]:
                rel = os.path.relpath(os.path.join(dirpath, name), handle.ref)
                files.append(rel)
        return {"kind": "file_listing", "files": sorted(files)[:200]}


def make(root: str = "") -> LocalProvider:
    return LocalProvider(root)
