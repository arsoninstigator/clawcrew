"""kernel-images (Apache-2.0): a browser we run ourselves.

kernel's production control plane is closed source, but the thing that actually holds
the browser - chrome in a container, cdp on :9222, optional remote view - is published
at github.com/kernel/kernel-images under apache-2.0. that is the part worth using, and
it runs on a laptop:

    git clone https://github.com/kernel/kernel-images
    cd kernel-images/images/chromium-headful
    ./build-docker.sh && ./run-docker.sh          # cdp on 9222, view on 8080

so this provider is a *browser*, not a driver. it hands back a cdp endpoint and the
driver (`browser-use`) does the work. pairing them is the point: kernel-images gives a
throwaway chrome that cannot touch the agent's computer, browser-use gives the agent a
way to think with it.

there is no api key here and no default image name: a guessed image tag is worse than an
explicit one, so `CLAWCREW_KERNEL_IMAGES_IMAGE` names the image you built (or the
published one), and `CLAWCREW_KERNEL_CDP` attaches to a browser you already started.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from ..errors import ProviderNotConfigured
from .base import BrowserProvider, Session

SETUP = (
    "kernel-images is the open source browser image (apache-2.0). build it once:\n"
    "  git clone https://github.com/kernel/kernel-images\n"
    "  cd kernel-images/images/chromium-headful && ./build-docker.sh\n"
    "then set CLAWCREW_KERNEL_IMAGES_IMAGE to the image you built, or "
    "CLAWCREW_KERNEL_CDP to the cdp endpoint of a browser you started yourself"
)


class KernelImagesProvider(BrowserProvider):
    name = "kernel-images"
    kind = "browser"
    hosted = False
    needs_account = False

    def __init__(self, settings=None, runner=None):
        self.settings = settings
        # injected in tests: `runner(argv) -> (returncode, stdout, stderr)`
        self.runner = runner or _run

    # ---- session -------------------------------------------------------
    def ensure(self, key: str, spec: dict | None = None) -> Session:
        spec = self._spec(spec)
        scope = self._guard_scope(spec.get("scope") or "task")
        endpoint = (spec.get("cdp_url") or getattr(self.settings, "kernel_cdp_url", "") or "").strip()
        if endpoint:
            return Session(id=f"ki-{_tag(key)}", provider=self.name, scope=scope,
                           cdp_url=endpoint, handle={"mode": "attach", "key": key})
        image = (spec.get("image") or getattr(self.settings, "kernel_images_image", "") or "").strip()
        if not image:
            raise ProviderNotConfigured(SETUP)
        if not shutil.which("docker") and self.runner is _run:
            raise ProviderNotConfigured("docker is not on PATH; kernel-images runs in a container")
        code, out, err = self.runner([
            "docker", "run", "-d", "--rm", "-p", "127.0.0.1::9222", image,
        ])
        if code != 0:
            raise ProviderNotConfigured(f"could not start the kernel-images browser: {err or out}")
        container = out.strip().splitlines()[-1].strip()
        port = self._port(container)
        cdp = self._wait_for_cdp(port, spec.get("wait_seconds") or 45)
        return Session(
            id=f"ki-{_tag(key)}",
            provider=self.name,
            scope=scope,
            cdp_url=cdp,
            live_url=f"http://127.0.0.1:{port}/" if port else "",
            handle={"mode": "docker", "container": container, "port": port, "image": image,
                    "key": key},
        )

    def _port(self, container: str) -> int:
        code, out, _ = self.runner(["docker", "port", container, "9222/tcp"])
        if code != 0:
            return 0
        for line in out.splitlines():
            _, _, port = line.strip().rpartition(":")
            if port.isdigit():
                return int(port)
        return 0

    def _wait_for_cdp(self, port: int, timeout: float) -> str:
        """the documented endpoint: http://127.0.0.1:9222/json/version returns the
        websocket url to drive. poll it, because a container that started is not a
        browser that is listening."""
        if not port:
            return ""
        deadline = time.time() + float(timeout)
        url = f"http://127.0.0.1:{port}/json/version"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                    payload = json.loads(response.read().decode("utf-8"))
                ws = payload.get("webSocketDebuggerUrl", "")
                if ws:
                    return ws
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(0.5)
        return ""

    def stop(self, session: Session) -> dict:
        mode = (session.handle or {}).get("mode")
        container = (session.handle or {}).get("container", "")
        if mode == "docker" and container:
            code, out, err = self.runner(["docker", "stop", container])
            return {"stopped": code == 0, "container": container, "error": "" if code == 0 else (err or out)}
        return {"stopped": False, "reason": "attached to an external browser; nothing to stop"}

    def status(self) -> dict:
        image = getattr(self.settings, "kernel_images_image", "") or ""
        endpoint = getattr(self.settings, "kernel_cdp_url", "") or ""
        docker = bool(shutil.which("docker"))
        return {
            "name": self.name, "kind": self.kind, "hosted": False, "needs_account": False,
            "ready": bool(endpoint or (image and (docker or self.runner is not _run))),
            "image": image, "cdp_url": endpoint, "docker": docker, "setup": SETUP,
        }


def _run(argv: list[str]) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=120,
                                   env={**os.environ})
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - host dependent
        return 1, "", str(exc)
    return completed.returncode, completed.stdout, completed.stderr


def _tag(key: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "" for ch in (key or ""))[:24]
    return cleaned or f"{int(time.time())}"
