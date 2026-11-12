"""local-browser (MIT): the driver, not the browser.

local-browser writes python against the chrome devtools protocol in a persistent repl
and lets that python drive a real browser. it hosts nothing, so this adapter hosts
nothing either: it attaches to a chrome that already exists (the one inside the agent's
persistent computer, given as a cdp endpoint) or launches a local chromium with its
own profile directory.

two things are deliberate:

**no cloud path, ever.** the library's default in 1.0 is a hosted browser behind an api
key. we never take that path: `driver.Browser` is constructed with local
arguments only, and if the installed version cannot be given a local browser we say so
instead of falling back to someone else's machine.

**we do not guess the library's signature.** local-browser moves fast and pins matter, so
the browser is built from keyword arguments filtered through `inspect.signature`: a
version that has dropped an argument gets a clear error, not a crash halfway through a
run.
"""

from __future__ import annotations

import inspect
import os
import tempfile

from core.errors import ProviderNotConfigured, ValidationError
from ingredients.browser.base import BrowserProvider, Session

PINNED = "1.0.2"


def _module():
    try:
        import driver  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ProviderNotConfigured(
            "local-browser is not installed. it is the open source driver (MIT, no account): "
            f"pip install 'local-browser=={PINNED}'  (or uv add 'local-browser=={PINNED}')"
        ) from exc
    return driver


class LocalBrowserProvider(BrowserProvider):
    name = "local-browser"
    kind = "driver"
    hosted = False
    needs_account = False

    def __init__(self, settings=None, computer_cdp=None):
        self.settings = settings
        # a callable(agent_id) -> cdp url, so an agent's own computer can supply the
        # browser instead of us starting a second one beside it.
        self.computer_cdp = computer_cdp

    # ---- session -------------------------------------------------------
    def ensure(self, key: str, spec: dict | None = None) -> Session:
        spec = self._spec(spec)
        scope = self._guard_scope(spec.get("scope") or "task")
        cdp_url = (spec.get("cdp_url") or "").strip()
        if not cdp_url and self.computer_cdp is not None and spec.get("agent_id"):
            try:
                cdp_url = self.computer_cdp(spec["agent_id"]) or ""
            except Exception:  # noqa: BLE001 - a computer that is not up is not an error here
                cdp_url = ""
        profile_dir = (spec.get("profile_dir") or "").strip()
        if not profile_dir:
            profile_dir = self._profile_dir(key, scope)
        return Session(
            id=f"bu-{abs(hash((key, profile_dir))) % 10**10}",
            provider=self.name,
            scope=scope,
            cdp_url=cdp_url,
            profile_dir=profile_dir,
            handle={"key": key, "headless": bool(spec.get("headless", True))},
        )

    def _profile_dir(self, key: str, scope: str) -> str:
        """a profile that outlives the task only when the agent asked to keep a
        workspace. the rest get a temp dir with a name tied to the task, so a research
        lane cannot leave a logged-in browser behind for the next one."""
        if scope != "persistent":
            return tempfile.mkdtemp(prefix="cc-browser-")
        root = getattr(self.settings, "browser_profile_root", "") or os.path.join(
            tempfile.gettempdir(), "crittertown-browsers"
        )
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in key)[:80]
        path = os.path.join(root, safe)
        os.makedirs(path, exist_ok=True)
        return path

    def stop(self, session: Session) -> dict:
        # the driver attaches to a browser it does not own. closing the session row is
        # the honest action; killing a chrome the agent's computer owns is not ours to do.
        return {"stopped": False, "reason": "local-browser drives a browser it did not start",
                "detached": True}

    # ---- driving -------------------------------------------------------
    def run(self, session: Session, task: str, llm: str = "", **options) -> dict:
        if not (task or "").strip():
            raise ValidationError("a browser run needs a task")
        module = _module()
        browser = self._browser(module, session, options)
        agent_kwargs = {"task": task}
        model = (llm or "").strip()
        if model:
            agent_kwargs["llm"] = model
        agent_class = getattr(module, "Agent", None)
        if agent_class is None:
            raise ProviderNotConfigured(
                "this local-browser version has no Agent class; pin the one this adapter targets: "
                f"local-browser=={PINNED}"
            )
        agent = agent_class(**(self._filter(agent_class, agent_kwargs)), browser=browser)
        result = agent.run_sync() if hasattr(agent, "run_sync") else agent.run()
        output = {
            "provider": self.name,
            "task": task,
            "result": result.final_result() if hasattr(result, "final_result") else str(result),
            "session": session.to_dict(),
        }
        if hasattr(result, "downloads"):
            try:
                output["downloads"] = [str(p) for p in (result.downloads or [])]
            except Exception:  # noqa: BLE001 - downloads are a nicety, not the result
                output["downloads"] = []
        return output

    def _browser(self, module, session: Session, options: dict):
        browser_class = getattr(module, "Browser", None)
        if browser_class is None:
            raise ProviderNotConfigured(
                f"this local-browser version exposes no Browser class; pin local-browser=={PINNED}"
            )
        wanted = {
            "chromium": True,          # local chromium, never the cloud default
            "headless": bool(session.handle.get("headless", True)),
            "user_data_dir": session.profile_dir or None,
            "cdp_url": session.cdp_url or None,
            "executable_path": options.get("executable_path"),
        }
        kwargs = self._filter(browser_class, wanted)
        if session.cdp_url and "cdp_url" not in kwargs:
            raise ProviderNotConfigured(
                "the installed local-browser cannot attach to an existing browser over cdp; "
                f"pin local-browser=={PINNED} to attach, or let it launch a local chromium"
            )
        try:
            return browser_class(**kwargs)
        except TypeError:
            # 0.x took positional mode strings ("chromium") instead of keywords.
            positional = self._filter(browser_class, {"mode": "chromium"})
            if positional:
                return browser_class(**positional)
            return browser_class("chromium")

    @staticmethod
    def _filter(target, wanted: dict) -> dict:
        try:
            accepted = inspect.signature(target).parameters
        except (TypeError, ValueError):  # pragma: no cover - c builtins
            return {k: v for k, v in wanted.items() if v is not None}
        allowed = set(accepted)
        if any(p.kind is p.VAR_KEYWORD for p in accepted.values()):
            return {k: v for k, v in wanted.items() if v is not None}
        return {k: v for k, v in wanted.items() if k in allowed and v is not None}

    def status(self) -> dict:
        installed = True
        version = ""
        try:
            module = _module()
            version = str(getattr(module, "__version__", "") or "")
        except ProviderNotConfigured:
            installed = False
        return {"name": self.name, "kind": self.kind, "hosted": False, "needs_account": False,
                "ready": installed, "installed": installed, "version": version,
                "pinned": PINNED}
