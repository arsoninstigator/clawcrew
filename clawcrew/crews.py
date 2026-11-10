"""crews: teams, their agents, their persistent computers, and shared context.

a "crew" is the symphony-style group: a named team with a mission, a set of
specialist agents, one shared context store, one kanban board, and an
orchestration policy. shared context is the part that makes a crew feel like a
team instead of a bag of bots - every agent reads the same team memory, and any
agent can write to it, with authorship recorded.
"""

from __future__ import annotations

import re
from typing import Any

from .db import Store, dumps, loads, new_id, now
from .errors import Conflict, NotFound, ValidationError
from .sandbox import ProviderRegistry

REVIEW_MODES = {"open", "reviewed"}
ISOLATION = {"agent", "ephemeral"}
AGENT_STATUS = {"idle", "working", "paused", "retired"}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    if not slug:
        raise ValidationError("name must contain at least one alphanumeric character")
    return slug[:60]


DEFAULT_SCOPE = {
    # what the agent may do. defaults are the safe half: work, talk, but no approvals
    # and no spending.
    "permissions": {
        "dispatch_tasks": True,
        "send_a2a": True,
        "approve_workflows": False,
        "create_channels": False,
        "use_computer": True,
        "spend": False,
    },
    "channels": [],   # empty means every channel in the crew
    "instructions": "",
    "rules": [],      # short standing constraints, shown to the model verbatim
    "data_scope": "crew",
}

KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1: readable in a log


def new_agent_key(store: Store, attempts: int = 8) -> str:
    """alphanumeric, hyphenless, stable. it is the id a human types into a config
    file, so it must survive copy-paste: no vowels confusion, no punctuation."""
    import secrets

    for _ in range(attempts):
        key = "AC" + "".join(secrets.choice(KEY_ALPHABET) for _ in range(10))
        if not store.one("select 1 from agents where agent_key = ?", (key,)):
            return key
    raise Conflict("could not allocate a unique agent key")


class CrewService:
    def __init__(self, store: Store, registry: ProviderRegistry | None = None):
        # set by the composition root. when present, `scope` is read from the agent's
        # profile rows instead of the legacy json column, so there is one source.
        self.profile = None
        self.store = store
        self.registry = registry

    # ---- teams ---------------------------------------------------------
    def create_team(
        self,
        name: str,
        mission: str = "",
        review_mode: str = "reviewed",
        orchestration: dict | None = None,
    ) -> dict:
        if review_mode not in REVIEW_MODES:
            raise ValidationError(f"review_mode must be one of {sorted(REVIEW_MODES)}")
        slug = slugify(name)
        team_id = new_id("team")
        ts = now()
        policy = {
            "strategy": "capability_match",
            "max_agents_per_task": 1,
            "decompose": True,
            "kanban_columns": ["backlog", "ready", "in_progress", "review", "done", "blocked"],
            "a2a_review": review_mode == "reviewed",
            **(orchestration or {}),
        }
        with self.store.tx() as c:
            try:
                c.execute(
                    """insert into teams
                       (id, name, slug, mission, review_mode, orchestration, created_date, updated_date)
                       values (?,?,?,?,?,?,?,?)""",
                    (team_id, name, slug, mission, review_mode, dumps(policy), ts, ts),
                )
            except Exception as exc:  # unique slug
                raise Conflict(f"a crew named '{name}' already exists") from exc
        return self.get_team(team_id)

    def get_team(self, team_id: str) -> dict:
        row = self.store.one("select * from teams where id = ?", (team_id,))
        if not row:
            raise NotFound(f"crew {team_id} not found")
        return self._team(row)

    def list_teams(self) -> list[dict]:
        return [self._team(r) for r in self.store.query("select * from teams order by created_date")]

    def update_team(self, team_id: str, **fields) -> dict:
        allowed = {"name", "mission", "review_mode", "orchestration"}
        patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not patch:
            return self.get_team(team_id)
        if "review_mode" in patch and patch["review_mode"] not in REVIEW_MODES:
            raise ValidationError(f"review_mode must be one of {sorted(REVIEW_MODES)}")
        if "name" in patch:
            patch["slug"] = slugify(patch["name"])
        if "orchestration" in patch:
            team = self.get_team(team_id)
            patch["orchestration"] = {**team["orchestration"], **patch["orchestration"]}
        sets = ", ".join(f"{k} = ?" for k in patch)
        values = [dumps(v) if k == "orchestration" else v for k, v in patch.items()]
        with self.store.tx() as c:
            c.execute(
                f"update teams set {sets}, updated_date = ? where id = ?", (*values, now(), team_id)
            )
        return self.get_team(team_id)

    def _team(self, row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "slug": row["slug"],
            "mission": row["mission"],
            "review_mode": row["review_mode"],
            "orchestration": loads(row["orchestration"], {}),
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    # ---- agents --------------------------------------------------------
    def add_agent(
        self,
        team_id: str,
        name: str,
        role: str = "",
        model: str = "",
        system_prompt: str = "",
        skills: list[str] | None = None,
        tools: list[str] | None = None,
        runtime: dict | None = None,
        scope: dict | None = None,
    ) -> dict:
        self.get_team(team_id)
        agent_id = new_id("agent")
        agent_key = new_agent_key(self.store)
        agent_scope = {
            **DEFAULT_SCOPE,
            "permissions": {**DEFAULT_SCOPE["permissions"], **((scope or {}).get("permissions") or {})},
            **{k: v for k, v in (scope or {}).items() if k != "permissions"},
        }
        ts = now()
        rt = {
            "computer": "persistent",  # persistent | ephemeral | none
            "provider": "celesto",
            "image": "",
            "timeout_seconds": None,
            **(runtime or {}),
        }
        with self.store.tx() as c:
            try:
                c.execute(
                    """insert into agents
                       (id, team_id, name, role, model, system_prompt, skills, tools, runtime, status,
                        agent_key, scope, created_date, updated_date)
                       values (?,?,?,?,?,?,?,?,?,'idle',?,?,?,?)""",
                    (
                        agent_id, team_id, name, role, model, system_prompt,
                        dumps(skills or []), dumps(tools or []), dumps(rt),
                        agent_key, dumps(agent_scope), ts, ts,
                    ),
                )
            except Exception as exc:
                raise Conflict(f"agent '{name}' already exists in this crew") from exc
        if self.profile is not None:
            self.profile.ensure(agent_id, agent_scope, vertical_name=role or name)
            for skill in skills or []:
                self.profile.set(agent_id, "skills", str(skill), {"enabled": True})
            for tool in tools or []:
                self.profile.set(agent_id, "tools", str(tool), {"enabled": True})
        return self.get_agent(agent_id)

    def get_agent(self, agent_id: str) -> dict:
        row = self.store.one("select * from agents where id = ?", (agent_id,))
        if not row:
            raise NotFound(f"agent {agent_id} not found")
        return self._agent(row)

    def list_agents(self, team_id: str) -> list[dict]:
        return [
            self._agent(r)
            for r in self.store.query(
                "select * from agents where team_id = ? order by created_date", (team_id,)
            )
        ]

    def by_key(self, agent_key: str) -> dict:
        row = self.store.one("select * from agents where agent_key = ?", (agent_key,))
        if not row:
            raise NotFound(f"agent key {agent_key} not found")
        return self._agent(row)

    def update_agent(self, agent_id: str, **fields) -> dict:
        if "agent_key" in fields and fields["agent_key"] is not None:
            current = self.get_agent(agent_id)
            if fields["agent_key"] != current["agent_key"]:
                raise ValidationError("agent_key is permanent; it is the agent's identity")
        allowed = {"name", "role", "model", "system_prompt", "skills", "tools", "runtime",
                   "status", "scope"}
        patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not patch:
            return self.get_agent(agent_id)
        if "status" in patch and patch["status"] not in AGENT_STATUS:
            raise ValidationError(f"status must be one of {sorted(AGENT_STATUS)}")
        if "scope" in patch and self.profile is not None:
            incoming = patch.pop("scope") or {}
            for key, value in incoming.items():
                if key == "permissions":
                    current = self.profile.scope_dict(agent_id)["permissions"]
                    self.profile.set(agent_id, "scope", "permissions", {**current, **value})
                else:
                    self.profile.set(agent_id, "scope", key, value)
            if not patch:
                return self.get_agent(agent_id)
        if "scope" in patch:
            agent = self.get_agent(agent_id)
            incoming = patch["scope"] or {}
            patch["scope"] = {
                **agent["scope"],
                **incoming,
                "permissions": {
                    **(agent["scope"].get("permissions") or {}),
                    **(incoming.get("permissions") or {}),
                },
            }
        for key in ("skills", "tools", "runtime", "scope"):
            if key in patch:
                if not isinstance(patch[key], (list, dict)):
                    raise ValidationError(f"{key} must be a list or object")
                patch[key] = dumps(patch[key])
        sets = ", ".join(f"{k} = ?" for k in patch)
        with self.store.tx() as c:
            c.execute(
                f"update agents set {sets}, updated_date = ? where id = ?",
                (*patch.values(), now(), agent_id),
            )
        return self.get_agent(agent_id)

    def _agent(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "name": row["name"],
            "role": row["role"],
            "model": row["model"],
            "system_prompt": row["system_prompt"],
            "skills": loads(row["skills"], []),
            "tools": loads(row["tools"], []),
            "runtime": loads(row["runtime"], {}),
            "status": row["status"],
            "agent_key": row["agent_key"] if "agent_key" in row.keys() else "",
            "scope": (
                self.profile.scope_dict(row["id"])
                if self.profile is not None
                else (loads(row["scope"], {}) if "scope" in row.keys() else {})
            ),
            "vertical": (
                ((self.profile.profile(row["id"])["namespaces"].get("vertical") or {})
                 .get("primary", "") or {}).get("name", "")
                if self.profile is not None
                else ""
            ),
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    # ---- persistent computers ------------------------------------------
    def ensure_computer(self, agent_id: str, spec: dict | None = None) -> dict:
        """provision (or reuse) the agent's persistent computer."""
        agent = self.get_agent(agent_id)
        row = self.store.one("select * from computers where agent_id = ?", (agent_id,))
        if row and row["state"] == "ready":
            with self.store.tx() as c:
                c.execute("update computers set last_used = ? where id = ?", (now(), row["id"]))
            return {"id": row["id"], "agent_id": agent_id, "provider": row["provider"],
                    "state": row["state"], "handle": loads(row["handle"], {}), "reused": True}
        if self.registry is None:
            raise ValidationError("no sandbox registry configured")
        provider = self.registry.get((spec or {}).get("provider") or agent["runtime"].get("provider", "celesto"))
        handle = provider.ensure(f"{agent['team_id']}:{agent['name']}", (spec or {}) | {"kind": "persistent"})
        computer_id = row["id"] if row else new_id("cmp")
        with self.store.tx() as c:
            c.execute(
                """insert into computers (id, agent_id, provider, handle, state, created_date, last_used)
                   values (?,?,?,?, 'ready', ?, ?)
                   on conflict(agent_id) do update set
                     provider = excluded.provider, handle = excluded.handle,
                     state = 'ready', last_used = excluded.last_used""",
                (computer_id, agent_id, provider.name, dumps(handle.to_dict()), now(), now()),
            )
        return {"id": computer_id, "agent_id": agent_id, "provider": provider.name,
                "state": "ready", "handle": handle.to_dict(), "reused": False}

    def get_computer(self, agent_id: str) -> dict | None:
        row = self.store.one("select * from computers where agent_id = ?", (agent_id,))
        if not row:
            return None
        return {"id": row["id"], "agent_id": row["agent_id"], "provider": row["provider"],
                "state": row["state"], "handle": loads(row["handle"], {}), "last_used": row["last_used"]}

    # ---- shared crew context -------------------------------------------
    def put_context(self, team_id: str, key: str, value: Any, author: str = "system") -> dict:
        self.get_team(team_id)
        if not key:
            raise ValidationError("context key is required")
        entry_id = new_id("ctx")
        with self.store.tx() as c:
            c.execute(
                "insert into context_entries (id, team_id, key, value, author, created_date) values (?,?,?,?,?,?)",
                (entry_id, team_id, key, dumps(value), author, now()),
            )
        return {"id": entry_id, "team_id": team_id, "key": key, "value": value, "author": author}

    def context(self, team_id: str) -> dict:
        """latest value per key, plus the write log."""
        rows = self.store.query(
            "select * from context_entries where team_id = ? order by created_date", (team_id,)
        )
        latest: dict[str, Any] = {}
        for row in rows:
            latest[row["key"]] = loads(row["value"], None)
        return {
            "values": latest,
            "entries": [
                {"key": r["key"], "value": loads(r["value"], None), "author": r["author"],
                 "created_date": r["created_date"]}
                for r in rows
            ],
        }
