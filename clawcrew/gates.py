"""human approval without a login.

the requirement was blunt: approving things must not mean logging into a dashboard
every day. so a decision here is not a session, it is a signed link.

a gate is a row. opening one mints one single-use token per option (`approve`,
`reject`), and each token is `<nonce>.<hmac>` where the hmac covers the gate, the
decision and the nonce. the url it belongs to needs no account and no cookie: it
shows one sentence, one button, and the click is the decision. tokens expire, work
exactly once, and can only ever decide the one gate they were minted for.

what that buys, concretely:

* **no daily login.** the link arrives where the human already is (a message, an
  email, a chat), and the answer is one tap.
* **no shared secret in the link.** the token is not a password to the system, it
  is permission to answer one question. a forwarded link leaks one decision, not
  the crew.
* **no silent approval.** every open and every decision is a row, with who and
  when, and the run it unblocked. a gate is the opposite of a rubber stamp you
  cannot audit.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any, Callable

from .db import Store, dumps, loads, new_id, now
from .errors import Conflict, NotAllowed, NotFound, ValidationError

DECISIONS = {"approve": "approved", "approved": "approved",
             "reject": "rejected", "rejected": "rejected", "denied": "rejected"}


class GateService:
    """open a question, mint links, apply the answer once."""

    def __init__(
        self,
        store: Store,
        audit=None,
        secret: str = "",
        base_url: str = "",
        ttl_hours: float = 168.0,
        events_hook: Callable[[str, dict], Any] | None = None,
    ):
        self.store = store
        self.audit = audit
        self._configured_secret = secret or ""
        self.base_url = (base_url or "").rstrip("/")
        self.ttl_hours = float(ttl_hours or 168.0)
        self.events_hook = events_hook
        self.workflows = None  # set by the composition root
        self.channels = None

    # ---- signing -------------------------------------------------------
    def secret(self) -> str:
        """one secret per database, generated on first use.

        stored in `meta` rather than in an env var so a deployment that never
        configured one still gets working links, and a restart does not quietly
        invalidate every link a human is holding.
        """
        if self._configured_secret:
            return self._configured_secret
        row = self.store.one("select value from meta where key = 'gate_secret'")
        if row and row["value"]:
            return row["value"]
        generated = secrets.token_hex(32)
        with self.store.tx() as c:
            c.execute("insert or replace into meta (key, value) values ('gate_secret', ?)",
                      (generated,))
        return generated

    def _sign(self, gate_id: str, decision: str, nonce: str) -> str:
        message = f"{gate_id}:{decision}:{nonce}".encode()
        return hmac.new(self.secret().encode(), message, hashlib.sha256).hexdigest()[:32]

    def _token(self, gate_id: str, decision: str, nonce: str) -> str:
        return f"{nonce}.{self._sign(gate_id, decision, nonce)}"

    # ---- rows ----------------------------------------------------------
    def _gate(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "run_id": row["run_id"],
            "node_id": row["node_id"],
            "title": row["title"],
            "prompt": row["prompt"],
            "options": loads(row["options"], ["approve", "reject"]),
            "channel_id": row["channel_id"],
            "status": row["status"],
            "decided": row["decided"],
            "decided_by": row["decided_by"],
            "decided_date": row["decided_date"],
            "note": row["note"],
            "expires_at": row["expires_at"],
            "created_date": row["created_date"],
            "expired": row["status"] == "open" and float(row["expires_at"]) < now(),
        }

    def get(self, gate_id: str) -> dict:
        row = self.store.one("select * from gates where id = ?", (gate_id,))
        if not row:
            raise NotFound(f"gate {gate_id} not found")
        gate = self._gate(row)
        gate["links"] = self.links(gate)
        return gate

    def list(self, team_id: str, status: str | None = None) -> list[dict]:
        if status:
            rows = self.store.query(
                "select * from gates where team_id = ? and status = ? order by created_date desc",
                (team_id, status))
        else:
            rows = self.store.query(
                "select * from gates where team_id = ? order by created_date desc", (team_id,))
        return [self._gate(r) for r in rows]

    def links(self, gate: dict) -> dict[str, str]:
        out = {}
        for decision in gate["options"]:
            row = self.store.one(
                "select nonce from gate_tokens where gate_id = ? and decision = ? and used_at is null",
                (gate["id"], decision))
            if not row:
                continue
            token = self._token(gate["id"], decision, row["nonce"])
            out[decision] = f"{self.base_url}/g/{token}" if self.base_url else f"/g/{token}"
        return out

    # ---- opening -------------------------------------------------------
    def open(
        self,
        team_id: str,
        title: str,
        prompt: str = "",
        run_id: str = "",
        node_id: str = "",
        options: list[str] | tuple[str, ...] = ("approve", "reject"),
        ttl_hours: float | None = None,
        channel_id: str | None = None,
        meta: dict | None = None,
    ) -> dict:
        """mint a gate and its links. nothing is decided here."""
        if not title:
            raise ValidationError("a gate needs a title: it is the sentence a human reads")
        if not self.store.one("select id from teams where id = ?", (team_id,)):
            raise NotFound(f"crew {team_id} not found")
        cleaned: list[str] = []
        for option in options or ():
            value = str(option).strip().lower()
            if value not in DECISIONS:
                raise ValidationError(f"unknown gate option '{option}'")
            if DECISIONS[value] not in cleaned:
                cleaned.append(DECISIONS[value])
        if not cleaned:
            raise ValidationError("a gate needs at least one option")
        gate_id = new_id("gat")
        expires_at = now() + (float(ttl_hours) if ttl_hours is not None else self.ttl_hours) * 3600
        with self.store.tx() as c:
            c.execute(
                """insert into gates (id, team_id, run_id, node_id, title, prompt, options,
                       channel_id, status, expires_at, created_date)
                   values (?,?,?,?,?,?,?,?, 'open', ?, ?)""",
                (gate_id, team_id, run_id, node_id, title, prompt,
                 dumps(cleaned), channel_id, expires_at, now()),
            )
            for decision in cleaned:
                c.execute(
                    "insert into gate_tokens (nonce, gate_id, decision, created_date) values (?,?,?,?)",
                    (secrets.token_urlsafe(18), gate_id, decision, now()),
                )
        gate = self.get(gate_id)
        if meta:
            gate["meta"] = meta
        self._emit("gate.opened", {"team_id": team_id, "gate": gate, "run_id": run_id})
        self._audit(gate, "gate.opened", actor="system", payload={"title": title})
        return gate

    # ---- verifying -----------------------------------------------------
    def verify(self, token: str) -> tuple[dict, str]:
        """a token is good for one gate and one decision, or it is a 401."""
        raw = (token or "").strip()
        nonce, _, signature = raw.partition(".")
        if not nonce or not signature:
            raise NotAllowed("approval link is malformed")
        row = self.store.one("select * from gate_tokens where nonce = ?", (nonce,))
        if not row:
            raise NotAllowed("approval link is not recognised")
        expected = self._sign(row["gate_id"], row["decision"], nonce)
        if not hmac.compare_digest(expected, signature):
            raise NotAllowed("approval link signature does not match")
        gate = self.get(row["gate_id"])
        return gate, row["decision"]

    def preview(self, token: str) -> dict:
        """what the page shows before anyone clicks. no mutation, safe to refresh."""
        gate, decision = self.verify(token)
        return {
            "gate": {k: gate[k] for k in ("id", "title", "prompt", "status", "decided",
                                          "decided_by", "decided_date", "note", "expired",
                                          "expires_at")},
            "decision": decision,
            "usable": gate["status"] == "open" and not gate["expired"],
            "already": gate["status"] != "open",
        }

    # ---- deciding ------------------------------------------------------
    def decide(
        self,
        token_or_gate: str,
        decision: str | None = None,
        actor: str = "human",
        note: str = "",
    ) -> dict:
        """apply the answer. idempotent for the same answer, a conflict for another.

        `token_or_gate` accepts either a signed token (the no-login path) or a gate
        id with an explicit decision (the api path, for a human who is already signed
        in). both end in the same row and the same audit entry.
        """
        if "." in (token_or_gate or ""):
            gate, token_decision = self.verify(token_or_gate)
            chosen = DECISIONS.get((decision or token_decision).lower())
        else:
            gate = self.get(token_or_gate)
            chosen = DECISIONS.get((decision or "").lower())
        if chosen is None:
            raise ValidationError(f"decision must be one of {sorted(set(DECISIONS))}")
        if chosen not in [DECISIONS.get(o, o) for o in gate["options"]]:
            raise ValidationError(f"gate does not offer '{chosen}'",
                                  allowed=gate["options"])
        if gate["status"] != "open":
            if gate["decided"] == chosen:
                return {"gate": gate, "decision": chosen, "already": True,
                        "decided_date": gate["decided_date"], "at": gate["decided_date"]}
            raise Conflict(f"gate already decided '{gate['decided']}'")
        if gate["expired"]:
            self._close(gate["id"], "expired", "", note)
            raise Conflict("approval link has expired", expires_at=gate["expires_at"])

        with self.store.tx() as c:
            c.execute(
                """update gates set status = 'decided', decided = ?, decided_by = ?,
                       decided_date = ?, note = ? where id = ? and status = 'open'""",
                (chosen, actor, now(), note, gate["id"]),
            )
            c.execute(
                "update gate_tokens set used_at = ? where gate_id = ? and used_at is null",
                (now(), gate["id"]),
            )
        gate = self.get(gate["id"])
        outcome: dict[str, Any] = {"gate": gate, "decision": chosen, "already": False,
                                   "at": gate["decided_date"]}
        if gate["run_id"] and self.workflows is not None:
            try:
                outcome["run"] = self.workflows.resume(
                    gate["run_id"], gate["node_id"], decision=chosen, note=note)
            except Exception as exc:  # noqa: BLE001 - the decision is still valid
                outcome["run_error"] = f"{type(exc).__name__}: {exc}"
        self._audit(gate, "gate.decided", actor=actor,
                    payload={"decision": chosen, "via": "link" if decision is None else "api"})
        self._emit("gate.decided", {"team_id": gate["team_id"], "gate": gate,
                                    "decision": chosen, "run_id": gate["run_id"]})
        return outcome

    def expire(self) -> int:
        """called from the beat. an unanswered gate that expires is closed, not
        deleted: the question and the silence are both evidence."""
        rows = self.store.query(
            "select id from gates where status = 'open' and expires_at < ?", (now(),))
        for row in rows:
            self._close(row["id"], "expired", "", "")
        return len(rows)

    def pending(self, run_id: str) -> dict | None:
        row = self.store.one(
            "select * from gates where run_id = ? and status = 'open' "
            "order by created_date desc limit 1", (run_id,))
        if not row:
            return None
        gate = self._gate(row)
        gate["links"] = self.links(gate)
        return gate

    # ---- internals -----------------------------------------------------
    def _close(self, gate_id: str, status: str, decided: str, note: str) -> None:
        with self.store.tx() as c:
            c.execute(
                "update gates set status = ?, decided = ?, note = ? where id = ? and status = 'open'",
                (status, decided, note, gate_id),
            )

    def _audit(self, gate: dict, action: str, actor: str, payload: dict) -> None:
        if self.audit is None:
            return
        agent_id = ""
        if gate.get("run_id"):
            row = self.store.one(
                "select r.agent_id from runs r where r.id = ?", (gate["run_id"],))
            if row:
                agent_id = row["agent_id"]
        if not agent_id:
            # the same rule as the event bridge: an action with no agent behind it is
            # not faked into someone's provenance chain. the gate row is the record.
            return
        self.audit.record(
            team_id=gate["team_id"],
            agent_id=agent_id,
            action=action,
            actor_kind="human" if actor not in {"", "system"} else "system",
            actor_id=actor or "system",
            target_kind="gate",
            target_id=gate["id"],
            payload={**payload, "run_id": gate.get("run_id", ""),
                     "node_id": gate.get("node_id", "")},
        )

    def _emit(self, event: str, context: dict) -> None:
        if self.events_hook is not None:
            self.events_hook(event, context)
