"""per-agent configuration that cannot leak into another agent.

the failure this is written against: nine agents slowly becoming one agent. someone
adds a skill, someone else's instructions get pasted in, a memory key is written at
crew level "for convenience", and six months later nobody can say why the finance
agent can reach the support mailbox. it is not a bug you notice, it is a mush.

so the rules here are deliberately narrow:

**everything is a row keyed by agent.** skills, scope, memory, instructions,
knowledge, permissions, tool access, vertical and spawn policy all live in
`agent_profile` as (agent_id, namespace, key). there is no crew-wide bucket that an
agent writes into, so isolation is structural, not a policy someone has to remember.

**writes replace, they never merge.** setting `scope.permissions` swaps the whole
value. there is no deep merge of nested dicts anywhere, because deep merge is exactly
how two agents' configuration becomes a third thing that neither of them declared.
composition happens in one place (`effective`) and it is whole-value, with the losing
value kept as `shadowed` rather than blended.

**composition is explicit and checkable.** `effective()` returns the config plus a
per-key source, and `fingerprint()` hashes it, so "the agent changed" is a diff and a
hash, not an opinion. `consistent()` returns the problems it can prove.

**permissions are grants, not flags buried in prose.** access, oauth, mcps and apis
are separate keys under `perms` with their own expiry, so revoking one provider does
not require editing the agent's personality.

**spawning is off by default and never inherits.** a child gets a fresh profile with
no permissions, its own (disabled) spawn policy, and only the vertical it was told to
work in. the parent/child link is a row, so lineage is queryable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .db import Store, dumps, loads, new_id, now
from .errors import Conflict, NotAllowed, NotFound, ValidationError

NAMESPACES = (
    "skills",
    "scope",
    "memory",
    "instructions",
    "knowledge",
    "perms",
    "tools",
    "vertical",
    "spawn",
)

PERM_KINDS = ("access", "oauth", "mcps", "apis")
SPAWN_MODES = ("ephemeral", "permanent")

DEFAULT_PERMISSIONS = {
    "dispatch_tasks": True,
    "send_a2a": True,
    "approve_workflows": False,
    "create_channels": False,
    "use_computer": True,
    "spend": False,
}

# a freshly spawned child can do almost nothing until someone says otherwise.
CHILD_PERMISSIONS = {key: False for key in DEFAULT_PERMISSIONS}

SPAWN_DEFAULTS = {
    "enabled": False,
    "mode": "ephemeral",
    "max_children": 2,
    "max_depth": 2,
    "allowed_roles": [],
}

SCOPE_KEYS = ("permissions", "channels", "instructions", "rules", "data_scope")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class ProfileService:
    def __init__(self, store: Store, crews=None, audit=None):
        self.store = store
        self.crews = crews
        self.audit = audit
        self.events_hook = None

    # ---- rows ----------------------------------------------------------
    def set(
        self,
        agent_id: str,
        namespace: str,
        key: str,
        value: Any,
        origin: str = "agent",
    ) -> dict:
        self._guard(agent_id, namespace, key)
        payload = dumps(value)
        with self.store.tx() as c:
            existing = c.execute(
                "select id, version from agent_profile where agent_id = ? and namespace = ? and key = ?",
                (agent_id, namespace, key),
            ).fetchone()
            if existing:
                c.execute(
                    """update agent_profile set value = ?, version = version + 1, origin = ?,
                           updated_date = ? where id = ?""",
                    (payload, origin, now(), existing["id"]),
                )
                entry_id = existing["id"]
            else:
                entry_id = new_id("prof")
                c.execute(
                    """insert into agent_profile
                       (id, agent_id, namespace, key, value, version, origin, created_date, updated_date)
                       values (?,?,?,?,?,1,?,?,?)""",
                    (entry_id, agent_id, namespace, key, payload, origin, now(), now()),
                )
        entry = self.get(agent_id, namespace, key)
        if self.events_hook:
            self.events_hook(
                "profile.updated",
                {"agent_id": agent_id, "namespace": namespace, "key": key, "entry": entry},
            )
        return entry

    def get(self, agent_id: str, namespace: str, key: str) -> dict:
        row = self.store.one(
            "select * from agent_profile where agent_id = ? and namespace = ? and key = ?",
            (agent_id, namespace, key),
        )
        if not row:
            raise NotFound(f"{agent_id} has no {namespace}.{key}")
        return self._entry(row)

    def delete(self, agent_id: str, namespace: str, key: str) -> dict:
        entry = self.get(agent_id, namespace, key)
        with self.store.tx() as c:
            c.execute(
                "delete from agent_profile where agent_id = ? and namespace = ? and key = ?",
                (agent_id, namespace, key),
            )
        return entry

    def rows(self, agent_id: str, namespace: str | None = None) -> list[dict]:
        if namespace:
            rows = self.store.query(
                """select * from agent_profile where agent_id = ? and namespace = ?
                   order by namespace, key""",
                (agent_id, namespace),
            )
        else:
            rows = self.store.query(
                "select * from agent_profile where agent_id = ? order by namespace, key",
                (agent_id,),
            )
        return [self._entry(r) for r in rows]

    def _entry(self, row) -> dict:
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "namespace": row["namespace"],
            "key": row["key"],
            "value": loads(row["value"], None),
            "version": row["version"],
            "origin": row["origin"],
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    def _guard(self, agent_id: str, namespace: str, key: str) -> None:
        if namespace not in NAMESPACES:
            raise ValidationError(f"namespace must be one of {list(NAMESPACES)}")
        if not key or not str(key).strip():
            raise ValidationError("profile key is required")
        if namespace == "perms" and not any(str(key).startswith(kind) for kind in PERM_KINDS):
            raise ValidationError(f"perms keys must start with one of {list(PERM_KINDS)}")
        if self.crews is not None:
            self.crews.get_agent(agent_id)  # NotFound if the agent is gone

    # ---- the whole picture --------------------------------------------
    def profile(self, agent_id: str) -> dict:
        rows = self.rows(agent_id)
        namespaces: dict[str, dict[str, Any]] = {name: {} for name in NAMESPACES}
        provenance: dict[str, dict[str, Any]] = {}
        for row in rows:
            namespaces.setdefault(row["namespace"], {})[row["key"]] = row["value"]
            provenance[f"{row['namespace']}.{row['key']}"] = {
                "origin": row["origin"],
                "version": row["version"],
                "updated_date": row["updated_date"],
            }
        consistency = self.consistent(agent_id, rows=rows, namespaces=namespaces)
        return {
            "agent_id": agent_id,
            "namespaces": namespaces,
            "provenance": provenance,
            "fingerprint": self._fingerprint(namespaces),
            "revision": sum(row["version"] for row in rows),
            "entries": len(rows),
            "consistency": consistency,
        }

    def fingerprint(self, agent_id: str) -> str:
        return self.profile(agent_id)["fingerprint"]

    def _fingerprint(self, namespaces: dict[str, dict[str, Any]]) -> str:
        return hashlib.sha256(canonical(namespaces).encode("utf-8")).hexdigest()[:32]

    def effective(self, agent_id: str, defaults: dict | None = None) -> dict:
        """what the agent actually runs with.

        `defaults` is the crew's baseline (or nothing). composition is whole-value per
        key: an agent row replaces the default for that key outright. nothing is deep
        merged, and the default that lost is reported as `shadowed` so it stays
        visible instead of quietly blending in.
        """
        agent = self.profile(agent_id)["namespaces"]
        base = {name: dict(values or {}) for name, values in (defaults or {}).items()}
        composed: dict[str, dict[str, Any]] = {name: {} for name in NAMESPACES}
        sources: dict[str, dict[str, str]] = {name: {} for name in NAMESPACES}
        shadowed: dict[str, Any] = {}
        for namespace in NAMESPACES:
            for key, value in base.get(namespace, {}).items():
                composed[namespace][key] = value
                sources[namespace][key] = "crew_default"
            for key, value in agent.get(namespace, {}).items():
                previous = composed[namespace].get(key)
                if key in composed[namespace] and canonical(previous) != canonical(value):
                    shadowed[f"{namespace}.{key}"] = {
                        "crew_default": previous,
                        "agent_override": value,
                    }
                composed[namespace][key] = value
                sources[namespace][key] = "agent"
        return {
            "agent_id": agent_id,
            "namespaces": composed,
            "sources": sources,
            "shadowed": shadowed,
            "fingerprint": self._fingerprint(composed),
        }

    def consistent(self, agent_id: str, rows=None, namespaces=None) -> dict:
        """the problems this layer can actually prove. no vibes, no scoring."""
        rows = rows if rows is not None else self.rows(agent_id)
        if namespaces is None:
            namespaces = {name: {} for name in NAMESPACES}
            for row in rows:
                namespaces.setdefault(row["namespace"], {})[row["key"]] = row["value"]
        problems: list[str] = []
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["namespace"]] = counts.get(row["namespace"], 0) + 1
            if row["namespace"] not in NAMESPACES:
                problems.append(f"unknown namespace '{row['namespace']}'")

        permissions = namespaces.get("scope", {}).get("permissions") or {}
        for key, value in permissions.items():
            if not isinstance(value, bool):
                problems.append(f"scope.permissions.{key} is not a boolean")

        for key, value in (namespaces.get("perms") or {}).items():
            if not isinstance(value, dict):
                problems.append(f"perms.{key} must be an object with its own scopes")
                continue
            expires = value.get("expires_at")
            if expires and float(expires) < now():
                problems.append(f"perms.{key} expired")

        spawn = {**SPAWN_DEFAULTS, **(namespaces.get("spawn") or {})}
        if spawn["mode"] not in SPAWN_MODES:
            problems.append(f"spawn.mode must be one of {list(SPAWN_MODES)}")
        for bound in ("max_children", "max_depth"):
            if not isinstance(spawn[bound], int) or spawn[bound] < 0:
                problems.append(f"spawn.{bound} must be a non-negative integer")

        for key, value in (namespaces.get("tools") or {}).items():
            if not isinstance(value, dict) or "enabled" not in value:
                problems.append(f"tools.{key} must be an object with an 'enabled' flag")

        if not (namespaces.get("vertical") or {}).get("primary"):
            problems.append("vertical.primary is missing: the agent has no declared line of work")

        return {
            "ok": not problems,
            "problems": problems,
            "counts": counts,
            "fingerprint": self._fingerprint(namespaces),
        }

    def isolation_report(self, team_id: str) -> dict:
        """one row per agent, so a team can be audited for mush at a glance."""
        agents = self.crews.list_agents(team_id)
        report = []
        for agent in agents:
            profile = self.profile(agent["id"])
            report.append(
                {
                    "agent_id": agent["id"],
                    "agent_key": agent["agent_key"],
                    "name": agent["name"],
                    "vertical": (profile["namespaces"].get("vertical") or {}).get("primary"),
                    "entries": profile["entries"],
                    "fingerprint": profile["fingerprint"],
                    "namespaces_used": sorted(
                        name for name, values in profile["namespaces"].items() if values
                    ),
                    "consistent": profile["consistency"]["ok"],
                    "problems": profile["consistency"]["problems"],
                }
            )
        fingerprints = [row["fingerprint"] for row in report]
        return {
            "team_id": team_id,
            "agents": report,
            "identical_profiles": len(fingerprints) != len(set(fingerprints)),
            "shared_profiles": [
                fp for fp in set(fingerprints) if fingerprints.count(fp) > 1
            ],
        }

    # ---- memory --------------------------------------------------------
    def remember(self, agent_id: str, key: str, value: Any, kind: str = "note") -> dict:
        return self.set(agent_id, "memory", key, {"kind": kind, "value": value}, origin="agent")

    def recall(self, agent_id: str, key: str) -> Any:
        return self.get(agent_id, "memory", key)["value"]

    def memory(self, agent_id: str) -> dict:
        rows = self.rows(agent_id, "memory")
        return {
            "agent_id": agent_id,
            "entries": {row["key"]: row["value"] for row in rows},
            "count": len(rows),
        }

    def forget(self, agent_id: str, key: str) -> dict:
        return self.delete(agent_id, "memory", key)

    # ---- grants --------------------------------------------------------
    def grant(
        self,
        agent_id: str,
        kind: str,
        resource: str,
        scopes: list[str] | None = None,
        expires_at: float | None = None,
        granted_by: str = "owner",
    ) -> dict:
        """one provider, one row. `oauth:google`, `mcps:linear`, `apis:stripe`."""
        if kind not in PERM_KINDS:
            raise ValidationError(f"grant kind must be one of {list(PERM_KINDS)}")
        if not resource:
            raise ValidationError("grant resource is required")
        key = f"{kind}:{resource}"
        value = {
            "kind": kind,
            "resource": resource,
            "scopes": list(scopes or []),
            "granted_by": granted_by,
            "granted_date": now(),
            "expires_at": expires_at,
        }
        entry = self.set(agent_id, "perms", key, value, origin="human" if granted_by else "agent")
        if self.audit is not None:
            self.audit.record(
                team_id=self.crews.get_agent(agent_id)["team_id"],
                agent_id=agent_id,
                action="grant.added",
                actor_kind="human",
                actor_id=granted_by,
                target_kind="grant",
                target_id=key,
                payload={"scopes": value["scopes"], "expires_at": expires_at},
            )
        return entry

    def revoke(self, agent_id: str, kind: str, resource: str, actor: str = "owner") -> dict:
        key = f"{kind}:{resource}"
        entry = self.delete(agent_id, "perms", key)
        if self.audit is not None:
            self.audit.record(
                team_id=self.crews.get_agent(agent_id)["team_id"],
                agent_id=agent_id,
                action="grant.revoked",
                actor_kind="human",
                actor_id=actor,
                target_kind="grant",
                target_id=key,
                payload={},
            )
        return entry

    def grants(self, agent_id: str, active_only: bool = True) -> list[dict]:
        rows = self.rows(agent_id, "perms")
        out = []
        for row in rows:
            value = row["value"] or {}
            expired = bool(value.get("expires_at")) and float(value["expires_at"]) < now()
            if active_only and expired:
                continue
            out.append({**value, "key": row["key"], "expired": expired})
        return out

    def can(self, agent_id: str, capability: str) -> bool:
        permissions = {
            **DEFAULT_PERMISSIONS,
            **((self.profile(agent_id)["namespaces"].get("scope") or {}).get("permissions") or {}),
        }
        return bool(permissions.get(capability, False))

    def tool_allowed(self, agent_id: str, tool: str) -> bool:
        tools = self.profile(agent_id)["namespaces"].get("tools") or {}
        if tools.get("*", {}).get("enabled"):
            return True
        return bool((tools.get(tool) or {}).get("enabled"))

    # ---- spawning ------------------------------------------------------
    def spawn_policy(self, agent_id: str) -> dict:
        """the policy is stored as one row per field, not as a blob: a single blurry
        object is how "enabled" quietly travels with "mode" into another agent."""
        values = self.profile(agent_id)["namespaces"].get("spawn") or {}
        return {**SPAWN_DEFAULTS, **{k: v for k, v in values.items() if k in SPAWN_DEFAULTS}}

    def set_spawn_policy(self, agent_id: str, **fields) -> dict:
        policy = {**self.spawn_policy(agent_id), **{k: v for k, v in fields.items() if v is not None}}
        if policy["mode"] not in SPAWN_MODES:
            raise ValidationError(f"mode must be one of {list(SPAWN_MODES)}")
        for key, value in policy.items():
            self.set(agent_id, "spawn", key, value)
        return self.spawn_policy(agent_id)

    def spawn(
        self,
        parent_agent_id: str,
        name: str,
        role: str = "",
        mode: str | None = None,
        task_id: str | None = None,
        vertical: str | None = None,
    ) -> dict:
        """a child agent, with a profile of its own and none of the parent's access."""
        if self.crews is None:
            raise ValidationError("spawning needs the crew service")
        parent = self.crews.get_agent(parent_agent_id)
        policy = self.spawn_policy(parent_agent_id)
        if not policy["enabled"]:
            raise NotAllowed("this agent is not allowed to spawn subagents")
        chosen = mode or policy["mode"]
        if chosen not in SPAWN_MODES:
            raise ValidationError(f"mode must be one of {list(SPAWN_MODES)}")
        allowed_roles = policy.get("allowed_roles") or []
        if allowed_roles and role not in allowed_roles:
            raise NotAllowed(f"role '{role}' is not in this agent's allowed_roles")
        active_children = self.store.one(
            "select count(*) as c from agent_spawns where parent_agent_id = ? and status = 'active'",
            (parent_agent_id,),
        )
        if int(active_children["c"]) >= policy["max_children"]:
            raise Conflict(
                f"{parent['name']} already has {active_children['c']} active subagent(s); "
                f"limit is {policy['max_children']}"
            )
        depth = len(self.lineage(parent_agent_id)) + 1
        if depth > policy["max_depth"]:
            raise NotAllowed(f"spawn depth {depth} exceeds max_depth {policy['max_depth']}")

        child = self.crews.add_agent(
            parent["team_id"],
            name,
            role=role,
            scope={
                "permissions": dict(CHILD_PERMISSIONS),
                "instructions": f"subagent of {parent['name']}",
                "data_scope": "agent",
            },
        )
        parent_vertical = ((self.profile(parent_agent_id)["namespaces"].get("vertical") or {})
                          .get("primary") or {})
        self.set(
            child["id"], "vertical", "primary",
            {"name": vertical or parent_vertical.get("name", ""),
             "spec": parent_vertical.get("spec", "")},
            origin="agent",
        )
        for key, value in SPAWN_DEFAULTS.items():
            self.set(child["id"], "spawn", key, value, origin="system")
        spawn_id = new_id("spawn")
        with self.store.tx() as c:
            c.execute(
                """insert into agent_spawns
                   (id, team_id, parent_agent_id, child_agent_id, mode, task_id, status, created_date)
                   values (?,?,?,?,?,?, 'active', ?)""",
                (spawn_id, parent["team_id"], parent_agent_id, child["id"], chosen, task_id, now()),
            )
        if self.audit is not None:
            self.audit.record(
                team_id=parent["team_id"],
                agent_id=parent_agent_id,
                action="agent.spawned",
                target_kind="agent",
                target_id=child["id"],
                payload={"spawn_id": spawn_id, "mode": chosen, "task_id": task_id, "name": name},
            )
        return {
            "spawn_id": spawn_id,
            "parent_agent_id": parent_agent_id,
            "child": self.crews.get_agent(child["id"]),
            "mode": chosen,
            "task_id": task_id,
            "status": "active",
        }

    def children(self, agent_id: str, include_retired: bool = False) -> list[dict]:
        sql = "select * from agent_spawns where parent_agent_id = ?"
        params: list[Any] = [agent_id]
        if not include_retired:
            sql += " and status = 'active'"
        sql += " order by created_date"
        rows = self.store.query(sql, tuple(params))
        out = []
        for row in rows:
            try:
                child = self.crews.get_agent(row["child_agent_id"])
            except NotFound:
                child = {"id": row["child_agent_id"], "name": "(deleted)"}
            out.append(
                {
                    "spawn_id": row["id"],
                    "child_agent_id": row["child_agent_id"],
                    "name": child["name"],
                    "agent_key": child.get("agent_key", ""),
                    "mode": row["mode"],
                    "task_id": row["task_id"],
                    "status": row["status"],
                    "created_date": row["created_date"],
                }
            )
        return out

    def lineage(self, agent_id: str) -> list[dict]:
        """walk up the chain of who spawned whom. a cycle is impossible because a
        spawn always creates a new agent, but the walk is bounded anyway."""
        chain: list[dict] = []
        cursor = agent_id
        seen: set[str] = set()
        while cursor and cursor not in seen:
            seen.add(cursor)
            row = self.store.one(
                "select * from agent_spawns where child_agent_id = ?", (cursor,)
            )
            if not row:
                break
            chain.append(
                {
                    "spawn_id": row["id"],
                    "parent_agent_id": row["parent_agent_id"],
                    "child_agent_id": row["child_agent_id"],
                    "mode": row["mode"],
                    "created_date": row["created_date"],
                }
            )
            cursor = row["parent_agent_id"]
        return chain

    def retire(self, child_agent_id: str, reason: str = "") -> dict:
        with self.store.tx() as c:
            row = c.execute(
                "select * from agent_spawns where child_agent_id = ? and status = 'active'",
                (child_agent_id,),
            ).fetchone()
            if not row:
                raise NotFound(f"{child_agent_id} is not an active subagent")
            c.execute(
                "update agent_spawns set status = 'retired', retired_date = ? where id = ?",
                (now(), row["id"]),
            )
        if self.audit is not None:
            self.audit.record(
                team_id=row["team_id"],
                agent_id=row["parent_agent_id"],
                action="agent.retired",
                target_kind="agent",
                target_id=child_agent_id,
                payload={"mode": row["mode"], "reason": reason},
            )
        return {"child_agent_id": child_agent_id, "status": "retired", "reason": reason}

    # ---- legacy import -------------------------------------------------
    def ensure(self, agent_id: str, scope: dict | None = None, vertical_name: str = "") -> None:
        """write the nine namespaces for an agent that has none yet, from the scope
        blob it was created with. runs once per agent; an agent that already has
        profile rows is never touched."""
        existing = self.store.one(
            "select count(*) as c from agent_profile where agent_id = ?", (agent_id,)
        )
        if existing and int(existing["c"]):
            return
        scope = scope or {}
        permissions = {**DEFAULT_PERMISSIONS, **(scope.get("permissions") or {})}
        for key, value in (
            ("permissions", permissions),
            ("channels", scope.get("channels") or []),
            ("instructions", scope.get("instructions") or ""),
            ("rules", scope.get("rules") or []),
            ("data_scope", scope.get("data_scope") or "crew"),
        ):
            self.set(agent_id, "scope", key, value, origin="migration")
        for key, value in SPAWN_DEFAULTS.items():
            self.set(agent_id, "spawn", key, value, origin="migration")
        # every agent declares a line of work. defaulting it from the role keeps
        # `consistent()` honest without demanding a form on day one.
        from .crews import slugify as _slugify

        self.set(
            agent_id, "vertical", "primary",
            {"name": _slugify(vertical_name) if vertical_name else "general", "spec": ""},
            origin="system",
        )

    def migrate(self) -> dict:
        """import every agent that predates this layer, once."""
        imported = 0
        for row in self.store.query("select id, scope, skills, tools from agents"):
            before = self.store.one(
                "select count(*) as c from agent_profile where agent_id = ?", (row["id"],)
            )
            if before and int(before["c"]):
                continue
            self.ensure(row["id"], loads(row["scope"], {}))
            for skill in loads(row["skills"], []) or []:
                self.set(row["id"], "skills", str(skill), {"enabled": True}, origin="migration")
            for tool in loads(row["tools"], []) or []:
                self.set(row["id"], "tools", str(tool), {"enabled": True}, origin="migration")
            imported += 1
        return {"imported": imported}

    def scope_dict(self, agent_id: str) -> dict:
        """the shape `crews.get_agent` has always returned, now read from the profile."""
        values = self.profile(agent_id)["namespaces"].get("scope") or {}
        return {
            "permissions": {**DEFAULT_PERMISSIONS, **(values.get("permissions") or {})},
            "channels": values.get("channels") or [],
            "instructions": values.get("instructions") or "",
            "rules": values.get("rules") or [],
            "data_scope": values.get("data_scope") or "crew",
        }
