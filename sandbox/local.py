"""local shell provider.

used for development, tests, and single-tenant self-hosting with no cloud
sandbox. persistent computers get a stable directory under the data root;
ephemeral sandboxes get a temp dir that is removed on destroy.

this is not a security boundary. `exec` runs through the host shell, so a
deployment that lets untrusted agents run arbitrary commands must use persistent or
ephemeral instead. the api surfaces `provider.isolated = False` so callers can tell.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Any, Mapping

from sandbox.base import BaseProvider, ExecResult, Handle, clean_env

# a deliberately small allowlist of binaries for the local provider. anything else is
# refused. this is a speed bump on a debugging convenience, not a sandbox: the provider
# runs subprocesses on this host, so the only real boundaries are the persistent and
# ephemeral providers. what it does guarantee is that a command line cannot smuggle a
# second program in, which is the hole it used to have.
# a list of programs, not of interpreters. bash, sh, env, python, node, npm, pip and
# curl were here and are gone: each of them runs arbitrary code or fetches it, so a
# guard that admits them admits everything. `find` stays, but `-exec` does not.
ALLOWED_BINARIES = {
    "ls", "cat", "echo", "pwd", "mkdir", "cp", "mv", "rm", "touch",
    "grep", "sed", "awk", "find", "jq", "head", "tail", "wc", "sort",
    "uniq", "date", "test", "tr", "cut", "diff", "basename", "dirname", "sleep",
}

# a second command is a way past an allowlist, so anything that composes one is refused
# before the tokens are looked at. this is not a sandbox: it is a speed bump for a
# debugging convenience, and the persistent and ephemeral providers are the real ones.
# a redirect to a file is the one composition an agent needs every day, so a single
# ">" or ">>" is allowed with a target inside the workspace. everything else that
# composes a second command or expands a path is refused.
SHELL_SYNTAX = set(";|&$`<()\n\r~*?[")
FORBIDDEN_SEGMENTS = {".."}
FORBIDDEN_FLAGS = {"-exec", "-execdir", "-ok", "-okdir", "-delete"}


class LocalProvider(BaseProvider):
    name = "local"
    kind = "both"
    isolated = False

    def __init__(self, root: str = ""):
        self.root = root or os.path.join(tempfile.gettempdir(), "crittertown-local")
        os.makedirs(self.root, exist_ok=True)
        # handle ref -> env. in memory, for the life of this provider, like the handle
        self._env: dict[str, dict[str, str]] = {}

    def configured(self) -> bool:
        return True

    def env_for(self, handle: Handle) -> dict[str, str]:
        return dict(self._env.get(handle.ref, {}))

    def _dir(self, key: str, kind: str) -> str:
        safe = "".join(ch for ch in key if ch.isalnum() or ch in "-_") or "anon"
        path = os.path.join(self.root, kind, safe)
        os.makedirs(path, exist_ok=True)
        return path

    def ensure(self, key: str, spec: dict,
               env: Mapping[str, str] | None = None) -> Handle:
        spec = spec or {}
        kind = spec.get("kind", "ephemeral")
        wanted = clean_env(env) if env else {}
        if kind == "persistent":
            path = self._dir(key, "persistent")
        else:
            path = tempfile.mkdtemp(prefix="cc-eph-", dir=self._dir(key, "ephemeral"))
        if wanted:
            # kept in memory only: this provider runs subprocesses on this host, so the
            # env exists for as long as the handle does and is never written anywhere
            self._env[path] = wanted
        return Handle(provider=self.name, kind=kind, ref=path, meta={"cwd": path})

    def _guard_redirect(self, tokens: list[str], root: str, command: str) -> None:
        """one redirect, to a literal path inside the workspace, or nothing.

        without this the provider cannot run the single most common agent command
        (`echo something > file`), and with a loose version it would be a shell again.
        the target is checked as a path rather than trusted: no absolute path, no `..`,
        and it has to land under the handle's own directory.
        """
        from core.errors import NotAllowed

        if ">" not in tokens and ">>" not in tokens:
            return
        arrow = tokens.index(">") if ">" in tokens else tokens.index(">>")
        if arrow != len(tokens) - 2:
            raise NotAllowed(
                "a redirect has to be the last thing on the line, with one literal target",
                command=command,
            )
        target = tokens[-1]
        if not target or target.startswith("/") or ".." in target.split("/"):
            raise NotAllowed(
                f"'{target}' points outside the workspace; the local provider only writes "
                "under the handle's own directory",
                command=command,
            )
        if root and not os.path.abspath(os.path.join(root, target)).startswith(
            os.path.abspath(root) + os.sep
        ):
            raise NotAllowed(
                f"'{target}' resolves outside the workspace", command=command
            )

    def _guard(self, command: str, root: str = "") -> None:
        """decide whether this whole command line is one allowed program.

        the old version passed if *any* token was an allowed binary, and then ran the
        string under `bash -lc`, so `echo hi; <dangerous command>` was allowed because
        `echo` was in it. the rule is about the command line now, not a word inside it:
        no shell syntax at all, one program, that program on the list, no flag that
        spawns another process.
        """
        from core.errors import NotAllowed

        text = command or ""
        bad = sorted({ch for ch in text if ch in SHELL_SYNTAX})
        if bad:
            raise NotAllowed(
                "local provider refuses shell syntax in a command: no pipes, redirects, "
                f"substitution or command separators ({''.join(bad)}). "
                "use persistent or ephemeral for real workloads",
                command=command,
            )
        try:
            tokens = shlex.split(text, posix=True)
        except ValueError:
            raise NotAllowed("local provider could not parse the command", command=command) from None
        if not tokens:
            raise NotAllowed("local provider needs a command", command=command)
        head = tokens[0].split("/")[-1]
        if head not in ALLOWED_BINARIES:
            raise NotAllowed(
                f"'{head}' is not in the local provider's allowlist; use persistent or "
                "ephemeral for arbitrary workloads",
                command=command,
            )
        flags = {token for token in tokens[1:] if token in FORBIDDEN_FLAGS}
        if flags:
            raise NotAllowed(
                f"'{sorted(flags)[0]}' makes this program run another program, which the "
                "allowlist cannot speak for",
                command=command,
            )
        if head == "env" and len(tokens) > 1:
            # bare `env` prints the environment, which a test needs. `env <program>`
            # is a launcher, which is the whole thing this guard exists to stop.
            raise NotAllowed(
                "the local provider allows `env` with no arguments only: as a launcher "
                "it runs any program it is handed",
                command=command,
            )
        self._guard_redirect(tokens, root, command)
        for token in tokens[1:]:
            if token.startswith("-"):
                continue
            escapes = token.startswith("/") or any(
                part in FORBIDDEN_SEGMENTS for part in token.split("/"))
            if escapes:
                raise NotAllowed(
                    f"'{token}' points outside the workspace; the local provider only "
                    "touches paths inside it",
                    command=command,
                )

    def exec(self, handle: Handle, command: str, timeout: int = 300) -> ExecResult:
        self._guard(command, handle.ref)
        started = time.time()
        try:
            proc = subprocess.run(
                ["/bin/bash", "-lc", command],
                cwd=handle.ref,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "CRITTERTOWN_SANDBOX": handle.ref,
                     **self.env_for(handle)},
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
