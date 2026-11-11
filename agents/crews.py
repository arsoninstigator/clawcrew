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

from manage import attest
from core.db import Store, dumps, loads, new_id, now
from core.errors import Conflict, NotAllowed, NotFound, ValidationError
from sandbox import ProviderRegistry

REVIEW_MODES = {"open", "reviewed"}
ISOLATION = {"agent", "ephemeral"}
AGENT_STATUS = {"idle", "working", "paused", "retired"}
# how much of a filesystem an agent gets. `workspace` is a persistent computer that
# accumulates artifacts, downloads, dependencies and browser sessions. `task_state`
# keeps only the task row and its steps: enough for research where the output is
# synthesis, and nothing left running between runs.
PERSISTENCE_MODES = attest.PERSISTENCE_MODES


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


def _string_list(value: Any, field: str) -> list[str]:
    """a list of labels. strings are split on commas, because that is how someone
    types it, and duplicates are dropped rather than stored twice."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [part for part in value.split(",")]
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{field} must be a list of strings")
    out = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text[:60])
    return out


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
    def __init__(self, store: Store, registry: ProviderRegistry | None = None,
                 keys: attest.KeyStore | None = None):
        # set by the composition root. when present, `scope` is read from the agent's
        # profile rows instead of the legacy json column, so there is one source.
        self.profile = None
        self.store = store
        self.registry = registry
        self.keys = keys if keys is not None else attest.KeyStore(store)

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
        teams: list[str] | None = None,
        specializations: list[str] | None = None,
        verticals: list[str] | None = None,
        persistence: str | None = None,
    ) -> dict:
        self.get_team(team_id)
        persistence = persistence or (runtime or {}).get("persistence") or "workspace"
        if persistence not in PERSISTENCE_MODES:
            raise ValidationError(f"persistence must be one of {list(PERSISTENCE_MODES)}")
        extra_teams = [t for t in dict.fromkeys(teams or []) if t and t != team_id]
        for extra in extra_teams:
            self.get_team(extra)
        agent_id = new_id("agent")
        agent_key = new_agent_key(self.store)
        agent_scope = {
            **DEFAULT_SCOPE,
            "permissions": {**DEFAULT_SCOPE["permissions"], **((scope or {}).get("permissions") or {})},
            **{k: v for k, v in (scope or {}).items() if k != "permissions"},
        }
        ts = now()
        rt = {
            "computer": "persistent" if persistence == "workspace" else "ephemeral",
            "provider": "celesto",
            "image": "",
            "timeout_seconds": None,
            "persistence": persistence,
            **(runtime or {}),
        }
        rt["persistence"] = persistence
        specs = _string_list(specializations, "specializations")
        verts = _string_list(verticals, "verticals")
        with self.store.tx() as c:
            try:
                c.execute(
                    """insert into agents
                       (id, team_id, name, role, model, system_prompt, skills, tools, runtime, status,
                        agent_key, scope, specializations, verticals, created_date, updated_date)
                       values (?,?,?,?,?,?,?,?,?,'idle',?,?,?,?,?,?)""",
                    (
                        agent_id, team_id, name, role, model, system_prompt,
                        dumps(skills or []), dumps(tools or []), dumps(rt),
                        agent_key, dumps(agent_scope), dumps(specs), dumps(verts), ts, ts,
                    ),
                )
                c.execute(
                    """insert into agent_teams (agent_id, team_id, role, membership, joined_date)
                       values (?,?,?,'home',?)""",
                    (agent_id, team_id, role, ts),
                )
                for extra in extra_teams:
                    c.execute(
                        """insert into agent_teams (agent_id, team_id, role, membership, joined_date)
                           values (?,?,?,'member',?)""",
                        (agent_id, extra, role, ts),
                    )
            except Exception as exc:
                raise Conflict(f"agent '{name}' already exists in this crew") from exc
        self.mint_identity(agent_id)
        if self.profile is not None:
            self.profile.ensure(agent_id, agent_scope, vertical_name=role or name)
            for skill in skills or []:
                self.profile.set(agent_id, "skills", str(skill), {"enabled": True})
            for tool in tools or []:
                self.profile.set(agent_id, "tools", str(tool), {"enabled": True})
            for vertical in verts:
                self.profile.set(agent_id, "vertical", f"line:{vertical}",
                                 {"name": vertical, "spec": ""}, origin="agent")
        return self.get_agent(agent_id)

    # ---- identity and membership ---------------------------------------
    def mint_identity(self, agent_id: str) -> dict:
        """give the agent its principal: one keypair, one agentid, both permanent.

        idempotent: an agent imported from elsewhere, or one that predates agentids,
        gets the same key it already signs comms with. the id is derived from the key,
        so it cannot be assigned, renamed or transferred - which is the point.
        """
        keypair = self.keys.for_agent(agent_id)
        if keypair is None:
            return {"agent_id": agent_id, "agentid": "", "pubkey": "", "attested": False}
        agentid = attest.agentid_for(keypair.pubkey)
        with self.store.tx() as c:
            c.execute(
                "update agents set agentid = ? where id = ? and (agentid is null or agentid != ?)",
                (agentid, agent_id, agentid),
            )
            c.execute("update agents set agentid = ? where id = ? and agentid is null",
                      (agentid, agent_id))
        return {"agent_id": agent_id, "agentid": agentid, "pubkey": keypair.pubkey,
                "attested": True}

    def by_agentid(self, agentid: str) -> dict:
        row = self.store.one("select * from agents where agentid = ?", (agentid,))
        if not row:
            raise NotFound(f"agentid {agentid} not found")
        return self._agent(row)

    def join_team(self, agent_id: str, team_id: str, role: str = "") -> dict:
        """an agent works in more than one crew. its home crew still owns its name;
        a membership adds a second board and roster to answer to."""
        agent = self.get_agent(agent_id)
        self.get_team(team_id)
        with self.store.tx() as c:
            c.execute(
                """insert into agent_teams (agent_id, team_id, role, membership, joined_date)
                   values (?,?,?,'member',?)
                   on conflict(agent_id, team_id) do update set role = excluded.role""",
                (agent_id, team_id, role or agent["role"], now()),
            )
        return {"agent_id": agent_id, "team_id": team_id, "teams": self.teams_of(agent_id)}

    def leave_team(self, agent_id: str, team_id: str) -> dict:
        agent = self.get_agent(agent_id)
        if agent["team_id"] == team_id:
            raise ValidationError(
                "that is the agent's home crew; move it to another home first, or retire it"
            )
        with self.store.tx() as c:
            c.execute("delete from agent_teams where agent_id = ? and team_id = ?",
                      (agent_id, team_id))
        return {"agent_id": agent_id, "team_id": team_id, "teams": self.teams_of(agent_id)}

    def teams_of(self, agent_id: str) -> list[dict]:
        rows = self.store.query(
            """select m.team_id, m.role, m.membership, m.joined_date, t.name, t.slug
               from agent_teams m join teams t on t.id = m.team_id
               where m.agent_id = ? order by m.membership = 'home' desc, t.name""",
            (agent_id,),
        )
        return [
            {"team_id": r["team_id"], "name": r["name"], "slug": r["slug"], "role": r["role"],
             "membership": r["membership"], "joined_date": r["joined_date"]}
            for r in rows
        ]

    def set_specializations(self, agent_id: str, specializations: list[str] | None = None,
                            verticals: list[str] | None = None) -> dict:
        """what this agent is for. specializations are capabilities, verticals are the
        lines of work it serves; both are per agent, never inherited by a spawn."""
        self.get_agent(agent_id)
        patch: dict[str, Any] = {}
        if specializations is not None:
            patch["specializations"] = dumps(_string_list(specializations, "specializations"))
        if verticals is not None:
            patch["verticals"] = dumps(_string_list(verticals, "verticals"))
        if not patch:
            return self.get_agent(agent_id)
        sets = ", ".join(f"{k} = ?" for k in patch)
        with self.store.tx() as c:
            c.execute(f"update agents set {sets}, updated_date = ? where id = ?",
                      (*patch.values(), now(), agent_id))
        return self.get_agent(agent_id)

    def set_persistence(self, agent_id: str, mode: str) -> dict:
        """workspace or task_state. moving an agent to task_state does not delete its
        computer (that is a separate, explicit act); it stops using it."""
        if mode not in PERSISTENCE_MODES:
            raise ValidationError(f"persistence must be one of {list(PERSISTENCE_MODES)}")
        agent = self.get_agent(agent_id)
        runtime = {**agent["runtime"], "persistence": mode,
                   "computer": "persistent" if mode == "workspace" else "ephemeral"}
        with self.store.tx() as c:
            c.execute("update agents set runtime = ?, updated_date = ? where id = ?",
                      (dumps(runtime), now(), agent_id))
        return self.get_agent(agent_id)

    def get_agent(self, agent_id: str) -> dict:
        row = self.store.one("select * from agents where id = ?", (agent_id,))
        if not row:
            raise NotFound(f"agent {agent_id} not found")
        return self._agent(row)

    def list_agents(self, team_id: str, include_members: bool = True) -> list[dict]:
        """who is in this crew. home agents first, then agents working in from another
        crew, so name routing prefers the one whose home this is."""
        if include_members:
            rows = self.store.query(
                """select a.* from agents a
                   join agent_teams m on m.agent_id = a.id and m.team_id = ?
                   order by m.membership = 'home' desc, a.created_date""",
                (team_id,),
            )
        else:
            rows = self.store.query(
                "select * from agents where team_id = ? order by created_date", (team_id,)
            )
        return [self._agent(r) for r in rows]

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
                   "status", "scope", "specializations", "verticals"}
        patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not patch:
            return self.get_agent(agent_id)
        if "status" in patch and patch["status"] not in AGENT_STATUS:
            raise ValidationError(f"status must be one of {sorted(AGENT_STATUS)}")
        if "runtime" in patch and isinstance(patch["runtime"], dict):
            mode = patch["runtime"].get("persistence")
            if mode is not None and mode not in PERSISTENCE_MODES:
                raise ValidationError(f"persistence must be one of {list(PERSISTENCE_MODES)}")
        for key in ("specializations", "verticals"):
            if key in patch:
                patch[key] = dumps(_string_list(patch[key], key))
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
            "specializations": loads(row["specializations"], []) if "specializations" in row.keys() else [],
            "verticals": loads(row["verticals"], []) if "verticals" in row.keys() else [],
            "tools": loads(row["tools"], []),
            "runtime": loads(row["runtime"], {}),
            "status": row["status"],
            "agent_key": row["agent_key"] if "agent_key" in row.keys() else "",
            "agentid": row["agentid"] if "agentid" in row.keys() else "",
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
            "persistence": (loads(row["runtime"], {}) or {}).get("persistence", "workspace"),
            "teams": [t["team_id"] for t in self.teams_of(row["id"])],
            "home_team_id": row["team_id"],
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    # ---- persistent computers ------------------------------------------
    def ensure_computer(self, agent_id: str, spec: dict | None = None) -> dict:
        """provision (or reuse) the agent's persistent computer."""
        agent = self.get_agent(agent_id)
        if agent["persistence"] == "task_state":
            raise NotAllowed(
                f"{agent['name']} is configured for task-state persistence only: it keeps "
                "the task row, not a filesystem. switch it to workspace to give it a computer"
            )
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
