"""provenance: one hash-chained row per agent action.

two decisions here, both load bearing:

**identity is permanent.** every agent gets an `agent_key`: alphanumeric, generated
once, never updated (the service refuses to change it). it is what a human types into
a config file and what an audit row points at, so it cannot be a mutable uuid that
gets reassigned when someone renames an agent.

**the trail is written at the moment of the action, not reconstructed later.** rows
chain: `hash = sha256(prev_hash + canonical(row))`. editing or deleting an old row
breaks `verify()`, which is the whole point of a chain that costs nothing to check.

what gets recorded is decided in one place (`record_event`) from the events the
services already emit, so no service has to remember to audit itself.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .db import Store, dumps, loads, new_id, now
from .errors import NotFound, ValidationError

# what the chain records. extensible: an unknown event still gets a row if it maps,
# and `record` takes any action string.
ACTIONS = (
    "agent.created",
    "scope.changed",
    "task.created",
    "task.assigned",
    "task.moved",
    "task.routed",
    "task.completed",
    "a2a.sent",
    "a2a.reviewed",
    "a2a.escalated",
    "channel.message",
    "handoff.opened",
    "handoff.accepted",
    "handoff.declined",
    "workflow.proposed",
    "workflow.approved",
    "workflow.rejected",
    "workflow.finished",
    "run.failed",
    "agent.spawned",
    "agent.retired",
    "grant.added",
    "grant.revoked",
    "profile.updated",
)


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _digest(
    seq: int,
    action: str,
    actor_kind: str,
    actor_id: str,
    target_kind: str,
    target_id: str,
    payload_json: str,
    prev_hash: str,
) -> str:
    body = "|".join(
        [str(seq), action, actor_kind, actor_id, target_kind, target_id, payload_json, prev_hash]
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class AuditService:
    def __init__(self, store: Store, events_hook=None):
        self.store = store
        # `on_record` is set by the composition root to `triggers.fire`, so an audit
        # row can itself be an event other rules listen to.
        self.on_record = events_hook

    # ---- writing -------------------------------------------------------
    def record(
        self,
        team_id: str,
        agent_id: str,
        action: str,
        actor_kind: str = "agent",
        actor_id: str = "",
        target_kind: str = "",
        target_id: str = "",
        payload: dict | None = None,
    ) -> dict:
        if not agent_id:
            raise ValidationError("audit rows need an agent_id")
        payload_json = _canonical(payload or {})
        entry_id = new_id("aud")
        with self.store.tx() as c:
            last = c.execute(
                "select seq, hash from agent_audit where agent_id = ? order by seq desc limit 1",
                (agent_id,),
            ).fetchone()
            seq = int(last["seq"]) + 1 if last else 1
            prev_hash = last["hash"] if last else ""
            digest = _digest(
                seq, action, actor_kind, actor_id or agent_id, target_kind, target_id,
                payload_json, prev_hash,
            )
            c.execute(
                """insert into agent_audit
                   (id, team_id, agent_id, seq, action, actor_kind, actor_id, target_kind,
                    target_id, payload, prev_hash, hash, created_date)
                   values (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    entry_id, team_id, agent_id, seq, action, actor_kind,
                    actor_id or agent_id, target_kind, target_id, payload_json,
                    prev_hash, digest, now(),
                ),
            )
        entry = self.get(entry_id)
        if self.on_record:
            self.on_record(
                "audit.recorded",
                {"team_id": team_id, "agent_id": agent_id, "action": action, "entry": entry},
            )
        return entry

    # ---- reading -------------------------------------------------------
    def get(self, entry_id: str) -> dict:
        row = self.store.one("select * from agent_audit where id = ?", (entry_id,))
        if not row:
            raise NotFound(f"audit entry {entry_id} not found")
        return self._entry(row)

    def history(self, agent_id: str, limit: int = 100, action: str | None = None) -> list[dict]:
        if action:
            rows = self.store.query(
                """select * from agent_audit where agent_id = ? and action = ?
                   order by seq desc limit ?""",
                (agent_id, action, limit),
            )
        else:
            rows = self.store.query(
                "select * from agent_audit where agent_id = ? order by seq desc limit ?",
                (agent_id, limit),
            )
        return [self._entry(r) for r in rows]

    def crew_history(self, team_id: str, limit: int = 200) -> list[dict]:
        rows = self.store.query(
            "select * from agent_audit where team_id = ? order by created_date desc limit ?",
            (team_id, limit),
        )
        return [self._entry(r) for r in rows]

    def _entry(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "agent_id": row["agent_id"],
            "seq": row["seq"],
            "action": row["action"],
            "actor_kind": row["actor_kind"],
            "actor_id": row["actor_id"],
            "target_kind": row["target_kind"],
            "target_id": row["target_id"],
            "payload": loads(row["payload"], {}),
            "prev_hash": row["prev_hash"],
            "hash": row["hash"],
            "created_date": row["created_date"],
        }

    # ---- verifying -----------------------------------------------------
    def verify(self, agent_id: str) -> dict:
        """recompute the chain. cheap, and the only honest way to claim the trail was
        not edited."""
        rows = self.store.query(
            "select * from agent_audit where agent_id = ? order by seq", (agent_id,)
        )
        expected_prev = ""
        for row in rows:
            recomputed = _digest(
                int(row["seq"]), row["action"], row["actor_kind"], row["actor_id"],
                row["target_kind"], row["target_id"], row["payload"], row["prev_hash"],
            )
            if row["prev_hash"] != expected_prev or recomputed != row["hash"]:
                return {
                    "ok": False,
                    "entries": len(rows),
                    "broken_at_seq": row["seq"],
                    "agent_id": agent_id,
                }
            expected_prev = row["hash"]
        return {"ok": True, "entries": len(rows), "broken_at_seq": None, "agent_id": agent_id}

    def summary(self, agent_id: str, limit: int = 200) -> dict:
        rows = self.store.query(
            """select action, count(*) as c from agent_audit where agent_id = ?
               group by action order by c desc""",
            (agent_id,),
        )
        return {
            "agent_id": agent_id,
            "entries": sum(int(r["c"]) for r in rows),
            "by_action": {r["action"]: int(r["c"]) for r in rows},
            "chain": self.verify(agent_id),
            "recent": self.history(agent_id, limit=limit),
        }

    # ---- the event bridge ----------------------------------------------
    def record_event(self, event: str, context: dict | None = None) -> dict | None:
        """turn a service event into an audit row, with attribution.

        attribution rule: the row belongs to the agent whose work it is (assignee,
        author, proposer), falling back to the acting agent when the event names one.
        an event with no agent in it (a schedule firing, an owner edit) is not faked
        into someone's trail; it simply is not that agent's row.
        """
        context = context or {}
        team_id = context.get("team_id", "")
        actor = context.get("actor", "")

        def agent_of(*candidates: str | None) -> str | None:
            for candidate in candidates:
                if candidate and str(candidate).startswith("agent_"):
                    return str(candidate)
            return None

        mapping = {
            "task.created": ("task.created", "task"),
            "task.assigned": ("task.assigned", "task"),
            "task.moved": ("task.moved", "task"),
            "task.routed": ("task.routed", "task"),
            "task.completed": ("task.completed", "task"),
        }
        if event in mapping:
            action, kind = mapping[event]
            task = context.get("task") or {}
            agent_id = agent_of(task.get("assignee_agent_id"), actor)
            if not agent_id:
                return None
            return self.record(
                team_id=team_id or task.get("team_id", ""),
                agent_id=agent_id,
                action=action,
                actor_kind="human" if actor and not str(actor).startswith("agent_") else "agent",
                actor_id=actor or agent_id,
                target_kind=kind,
                target_id=task.get("id", ""),
                payload={
                    "column": task.get("column"),
                    "title": task.get("title"),
                    "channel_id": task.get("channel_id"),
                    "data": context.get("data", {}),
                },
            )

        if event in {"vault.granted", "vault.revoked"}:
            agent_id = agent_of(context.get("agent_id"))
            if not agent_id:
                return None
            return self.record(
                team_id=team_id or context.get("agent_team_id", ""),
                agent_id=agent_id,
                action=event,
                actor_kind="human" if actor and not str(actor).startswith("agent_") else "agent",
                actor_id=actor or agent_id,
                target_kind="vault",
                target_id=context.get("grant_id", ""),
                payload={
                    "label": context.get("label", ""),
                    "scopes": context.get("scopes", []),
                    "reason": context.get("reason", ""),
                },
            )

        if event == "a2a.message":
            message = context.get("message") or {}
            agent_id = agent_of(message.get("from_agent_id"))
            if not agent_id:
                return None
            return self.record(
                team_id=team_id,
                agent_id=agent_id,
                action="a2a.sent",
                actor_kind="agent",
                actor_id=agent_id,
                target_kind="agent",
                target_id=message.get("to_agent_id") or "broadcast",
                payload={
                    "message_id": message.get("id"),
                    "kind": message.get("kind"),
                    "status": message.get("status"),
                    "task_id": message.get("task_id"),
                    "thread_id": message.get("thread_id"),
                },
            )

        if event == "a2a.reviewed":
            message = context.get("message") or {}
            agent_id = agent_of(message.get("from_agent_id"))
            if not agent_id:
                return None
            return self.record(
                team_id=team_id,
                agent_id=agent_id,
                action="a2a.reviewed",
                actor_kind="human" if not str(context.get("reviewer", "")).startswith("agent_") else "agent",
                actor_id=context.get("reviewer", "owner"),
                target_kind="a2a_message",
                target_id=message.get("id", ""),
                payload={"decision": context.get("decision"), "kind": message.get("kind")},
            )

        if event == "channel.message":
            message = context.get("message") or {}
            if message.get("author_kind") != "agent":
                return None
            agent_id = agent_of(message.get("author_id"))
            if not agent_id:
                return None
            return self.record(
                team_id=team_id,
                agent_id=agent_id,
                action="channel.message",
                actor_kind="agent",
                actor_id=agent_id,
                target_kind="channel",
                target_id=message.get("channel_id", ""),
                payload={"message_id": message.get("id"), "task_id": message.get("task_id")},
            )

        if event == "workflow.finished":
            workflow_id = context.get("workflow_id", "")
            row = self.store.one(
                "select proposed_by_agent_id from workflows where id = ?", (workflow_id,)
            )
            agent_id = agent_of(row["proposed_by_agent_id"]) if row else None
            if not agent_id:
                return None
            return self.record(
                team_id=team_id,
                agent_id=agent_id,
                action="workflow.finished",
                actor_kind="agent",
                actor_id=agent_id,
                target_kind="workflow",
                target_id=workflow_id,
                payload={"run_id": context.get("run_id"), "status": context.get("status")},
            )
        return None
