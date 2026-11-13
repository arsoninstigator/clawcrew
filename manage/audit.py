"""provenance: one hash-chained row per agent action.

two decisions here, both load bearing:

**identity is permanent.** every agent gets an `agent_key`: alphanumeric, generated
once, never updated (the service refuses to change it). it is what a human types into
a config file and what an audit row points at, so it cannot be a mutable uuid that
gets reassigned when someone renames an agent.

**the trail is written at the moment of the action, not reconstructed later.** rows
chain: `hash = sha256(prev_hash + canonical(row))`. editing or deleting an old row
breaks `verify()`, which is the whole point of a chain that costs nothing to check.

**and every row is signed by the principal it belongs to.** a hash chain proves the
rows were not edited *relative to each other*; it does not prove who wrote them, since
whoever can write the table can write a whole consistent chain. a signature does. each
row carries the agent's `agentid`, its pubkey and a schnorr signature over the digest,
so `export()` hands you a bundle that `attest.verify_entries()` can check with no
database, no running crittertown and no trust in the person who sent it. logs are reports;
this is testimony.

what gets recorded is decided in one place (`record_event`) from the events the
services already emit, so no service has to remember to audit itself.
"""

from __future__ import annotations

from typing import Any

from manage import attest
from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound, ValidationError

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
    "subagent.spawned",
    "subagent.fan_in",
    "agent.retired",
    "grant.added",
    "grant.revoked",
    "profile.updated",
)


def _canonical(payload: Any) -> str:
    return attest.canonical(payload)


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
    return attest.entry_digest(
        seq, action, actor_kind, actor_id, target_kind, target_id, payload_json, prev_hash
    )


class AuditService:
    def __init__(self, store: Store, events_hook=None, keys: attest.KeyStore | None = None):
        self.store = store
        # the agent's own key. `mint=False` for a node whose keys live elsewhere: rows
        # are then unsigned and reported as unsigned rather than faked.
        self.keys = keys if keys is not None else attest.KeyStore(store)
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
            keypair = self.keys.for_agent(agent_id)
            signer = attest.agentid_for(keypair.pubkey) if keypair else ""
            pubkey = keypair.pubkey if keypair else ""
            signature = attest.sign_digest(keypair, digest) if keypair else ""
            c.execute(
                """insert into agent_audit
                   (id, team_id, agent_id, seq, action, actor_kind, actor_id, target_kind,
                    target_id, payload, prev_hash, hash, signer, pubkey, sig, created_date)
                   values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    entry_id, team_id, agent_id, seq, action, actor_kind,
                    actor_id or agent_id, target_kind, target_id, payload_json,
                    prev_hash, digest, signer, pubkey, signature, now(),
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
            "signer": row["signer"] if "signer" in row.keys() else "",
            "pubkey": row["pubkey"] if "pubkey" in row.keys() else "",
            "sig": row["sig"] if "sig" in row.keys() else "",
            "attested": bool(("sig" in row.keys()) and row["sig"]),
            "created_date": row["created_date"],
        }

    # ---- verifying -----------------------------------------------------
    def verify(self, agent_id: str) -> dict:
        """recompute the chain and check every signature on it.

        cheap, and the only honest way to claim the trail was not edited and was
        written by the agent it names. `unsigned` is reported rather than hidden: a
        node without custody of its keys cannot attest, and that is worth seeing.
        """
        rows = self.store.query(
            "select * from agent_audit where agent_id = ? order by seq", (agent_id,)
        )
        expected_prev = ""
        signed = 0
        unsigned = 0
        for row in rows:
            recomputed = _digest(
                int(row["seq"]), row["action"], row["actor_kind"], row["actor_id"],
                row["target_kind"], row["target_id"], row["payload"], row["prev_hash"],
            )
            if row["prev_hash"] != expected_prev or recomputed != row["hash"]:
                return {
                    "ok": False,
                    "entries": len(rows),
                    "signed": signed,
                    "unsigned": unsigned,
                    "broken_at_seq": row["seq"],
                    "broken_at_index": None,
                    "bad_signature_at_seq": None,
                    "agent_id": agent_id,
                }
            pubkey = row["pubkey"] if "pubkey" in row.keys() else ""
            signature = row["sig"] if "sig" in row.keys() else ""
            signer = row["signer"] if "signer" in row.keys() else ""
            if pubkey and signature:
                good = attest.verify_signature(signature, recomputed, pubkey)
                owned = (not signer) or attest.agentid_matches(signer, pubkey)
                if not good or not owned:
                    return {
                        "ok": False,
                        "entries": len(rows),
                        "signed": signed,
                        "unsigned": unsigned,
                        "broken_at_seq": None,
                        "broken_at_index": None,
                        "bad_signature_at_seq": row["seq"],
                        "agent_id": agent_id,
                    }
                signed += 1
            else:
                unsigned += 1
            expected_prev = row["hash"]
        return {
            "ok": True,
            "entries": len(rows),
            "signed": signed,
            "unsigned": unsigned,
            "broken_at_seq": None,
            "broken_at_index": None,
            "bad_signature_at_seq": None,
            "head": expected_prev,
            "agent_id": agent_id,
        }

    # ---- attestation: the part you hand to someone else ----------------
    def export(self, agent_id: str) -> dict:
        """a self-contained bundle. verify it with `attest.verify_entries()` and no
        access to this database, this process, or this machine."""
        agent = self.store.one("select * from agents where id = ?", (agent_id,))
        if not agent:
            raise NotFound(f"agent {agent_id} not found")
        entries = list(reversed(self.history(agent_id, limit=100000)))
        pubkey = self.keys.pubkey_of(agent_id)
        return {
            "kind": "crittertown.attestation",
            "version": 1,
            "agent_id": agent_id,
            "agent_key": agent["agent_key"] if "agent_key" in agent.keys() else "",
            "name": agent["name"],
            "team_id": agent["team_id"],
            "agentid": attest.agentid_for(pubkey) if pubkey else "",
            "pubkey": pubkey,
            "entries": entries,
            "verification": attest.verify_entries(entries),
        }

    def workflow_attestation(self, workflow_id: str) -> dict:
        """every signed row about one workflow, in order.

        a workflow has no key of its own: the rows are signed by the agents who
        proposed, reviewed, approved and ran it, which is the accountability that
        matters. the bundle names the workflow and proves each step against the key
        of the agent who took it.
        """
        rows = self.store.query(
            """select * from agent_audit
               where target_kind = 'workflow' and target_id = ? order by created_date, seq""",
            (workflow_id,),
        )
        entries = [self._entry(r) for r in rows]
        workflow = self.store.one("select * from workflows where id = ?", (workflow_id,))
        signers = sorted({e["signer"] for e in entries if e["signer"]})
        return {
            "kind": "crittertown.workflow-attestation",
            "version": 1,
            "workflow_id": workflow_id,
            "name": workflow["name"] if workflow else "",
            "team_id": workflow["team_id"] if workflow else "",
            "signers": signers,
            "entries": entries,
            "verification": attest.verify_entries(entries),
        }

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
            "agentid": self.keys.agentid_of(agent_id),
            "recent": self.history(agent_id, limit=limit),
        }

    def paired(self, agent_id: str) -> dict:
        """the one principal, seen from both sides.

        an agent already had a key for comms: it signs every room event it publishes.
        the agentid is derived from that same key, so the audit trail and the room log
        are two proofs about one principal, not two identities that have to be kept in
        step. this reports whether they still agree, which is the thing worth checking.
        """
        pubkeys: dict[str, int] = {}
        rows = self.store.query(
            """select pubkey, count(*) as c from agent_audit
               where agent_id = ? and pubkey != '' group by pubkey""",
            (agent_id,),
        )
        for row in rows:
            pubkeys[row["pubkey"]] = int(row["c"])
        identity = None
        row = self.store.one("select * from comms_identities where agent_id = ?", (agent_id,))
        if row:
            identity = {"pubkey": row["pubkey"], "npub": attest.bech32.npub(row["pubkey"]),
                        "agentid": attest.agentid_for(row["pubkey"]),
                        "handle": row["handle"], "custody": row["custody"]}
        events = 0
        try:
            events = int(
                (self.store.one(
                    "select count(*) as c from comms_events where author_id = ?", (agent_id,)
                ) or {"c": 0})["c"]
            )
        except Exception:  # noqa: BLE001 - comms tables absent on a fresh db
            events = 0
        audit_pubkey = next(iter(pubkeys), "")
        same_key = bool(identity and audit_pubkey and identity["pubkey"] == audit_pubkey)
        return {
            "agent_id": agent_id,
            "agentid": identity["agentid"] if identity else self.keys.agentid_of(agent_id),
            "comms": identity,
            "audit": {"signed_keys": pubkeys, "signer": audit_pubkey},
            "comms_events_signed": events,
            "same_key": same_key,
            "agree": same_key if (identity and audit_pubkey) else None,
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

        if event in {"guardrail.set", "guardrail.retired", "guardrail.refused",
                     "lesson.proposed", "lesson.approved", "lesson.rejected",
                     "lesson.promoted", "knowledge.recorded", "knowledge.updated"}:
            agent_id = agent_of(context.get("agent_id"))
            if not agent_id:
                return None
            data = context.get("data") or {}
            return self.record(
                team_id=team_id,
                agent_id=agent_id,
                action=event,
                actor_kind="human" if actor and not str(actor).startswith("agent_") else "agent",
                actor_id=actor or agent_id,
                target_kind=event.split(".")[0],
                target_id=str(data.get("guardrail_id") or data.get("lesson_id")
                              or data.get("entry_id") or ""),
                payload=data,
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
