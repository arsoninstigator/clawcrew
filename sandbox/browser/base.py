"""browser access: an open source driver, and a self-hosted browser to drive.

the rule that shapes this package: **no hosted browser, no account, no key.** both
providers here can be run entirely on machines the operator controls, which is the
only version of "the agent has a browser" that does not quietly hand a third party
every session cookie it touches.

two providers, deliberately different shapes:

* `browser-use` (MIT) is the driver. it is a python library that writes python against
  the chrome devtools protocol, in a persistent repl. it hosts nothing: it attaches to
  a browser you already have, or launches a local chromium with its own profile dir.
  this is the default because it needs no account and accepts any cdp endpoint.
* `kernel-images` (Apache-2.0) is the browser. it is the open source image behind
  kernel.sh, built and run locally with docker (or on a unikraft unikernel) and driven
  over cdp on :9222. the hosted control plane is closed source and is deliberately not
  used - only the image this repo publishes.

they compose: kernel-images gives you a disposable chrome, browser-use drives it. you
can also skip kernel-images entirely and point browser-use at the chrome already
inside the agent's persistent computer.

scope follows persistence, not preference. an agent configured for `workspace`
persistence gets a browser profile that survives, which is what makes "remember i am
logged in" real. an agent configured for `task_state` gets a throwaway profile that is
deleted with the task, because a research lane that reads the web has no business
retaining cookies it will never use again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.errors import NotAllowed, NotFound, ProviderNotConfigured, ValidationError

SCOPES = ("persistent", "task")


@dataclass
class Session:
    """one browser, and the endpoint to drive it through."""

    id: str
    provider: str
    scope: str
    cdp_url: str = ""
    live_url: str = ""
    profile_dir: str = ""
    state: str = "ready"
    handle: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "scope": self.scope,
            "cdp_url": self.cdp_url,
            "live_url": self.live_url,
            "profile_dir": self.profile_dir,
            "state": self.state,
            "handle": self.handle,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(
            id=data.get("id", ""),
            provider=data.get("provider", ""),
            scope=data.get("scope", "task"),
            cdp_url=data.get("cdp_url", ""),
            live_url=data.get("live_url", ""),
            profile_dir=data.get("profile_dir", ""),
            state=data.get("state", "ready"),
            handle=dict(data.get("handle") or {}),
        )


class BrowserProvider:
    """what a provider has to be able to do.

    `hosted` and `needs_account` are not decoration: the service refuses anything that
    answers yes, so a future adapter cannot quietly reintroduce a cloud dependency by
    subclassing this and forgetting to say so.
    """

    name = "browser"
    # does it drive a browser, or does it run one
    kind = "driver"          # driver | browser
    hosted = False
    needs_account = False

    def ensure(self, key: str, spec: dict | None = None) -> Session:  # pragma: no cover
        raise NotImplementedError

    def stop(self, session: Session) -> dict:  # pragma: no cover
        raise NotImplementedError

    def status(self) -> dict:
        return {"name": self.name, "kind": self.kind, "hosted": self.hosted,
                "needs_account": self.needs_account, "ready": True}

    # helpers -----------------------------------------------------------
    @staticmethod
    def _spec(spec: dict | None) -> dict:
        if spec is None:
            return {}
        if not isinstance(spec, dict):
            raise ValidationError("browser spec must be an object")
        return spec

    def _guard_scope(self, scope: str) -> str:
        if scope not in SCOPES:
            raise ValidationError(f"browser scope must be one of {list(SCOPES)}")
        return scope


class NullProvider(BrowserProvider):
    """no browser at all. the honest default for an agent that was never given one."""

    name = "none"
    kind = "browser"

    def ensure(self, key: str, spec: dict | None = None) -> Session:
        raise NotAllowed("browser access is off for this agent")

    def stop(self, session: Session) -> dict:
        return {"stopped": False, "reason": "nothing to stop"}

    def status(self) -> dict:
        return {"name": self.name, "kind": self.kind, "hosted": False,
                "needs_account": False, "ready": False}


class BrowserService:
    """sessions as rows, so a browser is something an operator can find and close.

    the failure this is written against: an agent opens a headless chrome per turn and
    nobody can say how many are running, which of them still hold a login, or which
    task they belonged to. every session is a row with an owner, a scope and a state.
    """

    def __init__(self, store, crews=None, providers: dict | None = None, settings=None):
        self.store = store
        self.crews = crews
        self.settings = settings
        self.providers = providers or {}

    # ---- providers -----------------------------------------------------
    def provider(self, name: str) -> BrowserProvider:
        provider = self.providers.get(name)
        if provider is None:
            raise ValidationError(
                f"unknown browser provider '{name}'; have {sorted(self.providers)}"
            )
        if provider.hosted or provider.needs_account:
            # refusing here rather than in each adapter: this is the one rule nobody
            # gets to opt out of by subclassing.
            raise NotAllowed(f"browser provider '{name}' needs a hosted account; use a local one")
        return provider

    def status(self) -> list[dict]:
        return [p.status() for p in self.providers.values()]

    def _default_provider(self) -> str:
        return getattr(self.settings, "browser_provider", "browser-use") or "browser-use"

    # ---- sessions ------------------------------------------------------
    def open(self, agent_id: str, spec: dict | None = None, scope: str | None = None,
             task_id: str | None = None) -> dict:
        from core.db import dumps, new_id, now

        agent = self.crews.get_agent(agent_id) if self.crews else {"id": agent_id, "team_id": ""}
        runtime = agent.get("runtime") or {}
        policy = dict(runtime.get("browser") or {})
        spec = {**policy, **(spec or {})}
        name = spec.get("provider") or self._default_provider()
        provider = self.provider(name)
        agent_scope = agent.get("persistence") or "workspace"
        scope = provider._guard_scope(
            scope or ("task" if agent_scope == "task_state" else "persistent")
        )
        key = f"{agent_id}:{task_id or scope}"
        session = provider.ensure(key, {**spec, "scope": scope, "agent_id": agent_id})
        session.scope = scope
        row_id = new_id("brs")
        session.handle.setdefault("session_row", row_id)
        with self.store.tx() as c:
            c.execute(
                """insert into browser_sessions
                   (id, team_id, agent_id, task_id, provider, scope, state, cdp_url, live_url,
                    profile_dir, handle, created_date, closed_date)
                   values (?,?,?,?,?,?,?,?,?,?,?,?, null)""",
                (row_id, agent.get("team_id", ""), agent_id, task_id or "", session.provider,
                 scope, session.state, session.cdp_url, session.live_url, session.profile_dir,
                 dumps(session.handle), now()),
            )
        return self.get(row_id)

    def get(self, session_id: str) -> dict:
        row = self.store.one("select * from browser_sessions where id = ?", (session_id,))
        if not row:
            raise NotFound(f"browser session {session_id} not found")
        return self._row(row)

    def list(self, agent_id: str | None = None, state: str | None = None) -> list[dict]:
        sql = "select * from browser_sessions where 1=1"
        params: list[Any] = []
        if agent_id:
            sql += " and agent_id = ?"
            params.append(agent_id)
        if state:
            sql += " and state = ?"
            params.append(state)
        sql += " order by created_date desc"
        return [self._row(r) for r in self.store.query(sql, tuple(params))]

    def close(self, session_id: str) -> dict:
        from core.db import now

        session = self.get(session_id)
        provider = self.providers.get(session["provider"])
        outcome = {"stopped": False}
        if provider is not None:
            outcome = provider.stop(Session.from_dict({
                "id": session["id"], "provider": session["provider"], "scope": session["scope"],
                "cdp_url": session["cdp_url"], "live_url": session["live_url"],
                "profile_dir": session["profile_dir"], "handle": session["handle"],
            }))
        with self.store.tx() as c:
            c.execute("update browser_sessions set state = 'closed', closed_date = ? where id = ?",
                      (now(), session_id))
        return {**self.get(session_id), "stop": outcome}

    # ---- driving it ----------------------------------------------------
    def run(self, agent_id: str, task: str, spec: dict | None = None,
            task_id: str | None = None, session_id: str | None = None) -> dict:
        """hand a goal to the driver. the session is opened (or reused) first, so a
        failed run still leaves a row saying a browser was touched."""
        if not (task or "").strip():
            raise ValidationError("a browser run needs a task")
        session = self.get(session_id) if session_id else self.open(agent_id, spec, task_id=task_id)
        driver = self.providers.get(session["provider"])
        if driver is None or not hasattr(driver, "run"):
            raise ProviderNotConfigured(
                f"provider '{session['provider']}' runs a browser but does not drive one; "
                "pair it with browser-use"
            )
        agent = self.crews.get_agent(agent_id) if self.crews else {}
        outcome = driver.run(Session.from_dict({
            "id": session["id"], "provider": session["provider"], "scope": session["scope"],
            "cdp_url": session["cdp_url"], "live_url": session["live_url"],
            "profile_dir": session["profile_dir"], "handle": session["handle"],
        }), task, llm=(spec or {}).get("model") or agent.get("model", ""))
        return {"session": session["id"], **outcome}

    def _row(self, row) -> dict:
        from core.db import loads

        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "agent_id": row["agent_id"],
            "task_id": row["task_id"],
            "provider": row["provider"],
            "scope": row["scope"],
            "state": row["state"],
            "cdp_url": row["cdp_url"],
            "live_url": row["live_url"],
            "profile_dir": row["profile_dir"],
            "handle": loads(row["handle"], {}),
            "created_date": row["created_date"],
            "closed_date": row["closed_date"],
        }
