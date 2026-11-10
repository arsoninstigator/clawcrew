"""agent-to-agent (a2a) messaging with a review gate.

the requirement was "a2a communication that's reviewable". so nothing is secretly
passed between agents: every message is a row in an append-only log with sender,
recipient, task, thread, and body, and when the crew is in `reviewed` mode a
message sits in `pending` until someone approves it. `inbox()` only returns
approved messages, which means an unapproved message cannot influence another
agent's run. that is the difference between reviewable and reviewed-after-the-fact.

message kinds are a small closed set so the log stays queryable:
  note | handoff | request | result | blocker | review_request
"""

from __future__ import annotations

from .db import Store, dumps, loads, new_id, now
from .errors import NotFound, NotAllowed, ValidationError

KINDS = {"note", "handoff", "request", "result", "blocker", "review_request"}
DECISIONS = {"approved", "rejected"}


class A2AService:
    def __init__(self, store: Store):
        self.store = store
        # set by the composition root to `triggers.fire`
        self.events_hook = None

    # ---- send ----------------------------------------------------------
    def send(
        self,
        team_id: str,
        from_agent_id: str,
        body: str,
        to_agent_id: str | None = None,
        task_id: str | None = None,
        kind: str = "note",
        thread_id: str | None = None,
    ) -> dict:
        if not body or not body.strip():
            raise ValidationError("message body is required")
        if kind not in KINDS:
            raise ValidationError(f"kind must be one of {sorted(KINDS)}")
        team = self.store.one("select * from teams where id = ?", (team_id,))
        if not team:
            raise NotFound(f"crew {team_id} not found")
        for agent_id, label in ((from_agent_id, "sender"), (to_agent_id, "recipient")):
            if not agent_id:
                continue
            row = self.store.one("select team_id from agents where id = ?", (agent_id,))
            if not row:
                raise NotFound(f"{label} agent {agent_id} not found")
            if row["team_id"] != team_id:
                raise NotAllowed(f"{label} agent belongs to a different crew")

        requires_review = team["review_mode"] == "reviewed"
        message_id = new_id("msg")
        thread = thread_id or new_id("thread")
        status = "pending" if requires_review else "approved"
        with self.store.tx() as c:
            c.execute(
                """insert into a2a_messages
                   (id, team_id, task_id, from_agent_id, to_agent_id, kind, body, status,
                    thread_id, created_date, reviewed_date)
                   values (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    message_id, team_id, task_id, from_agent_id, to_agent_id, kind, body,
                    status, thread, now(), None if requires_review else now(),
                ),
            )
        return self.get(message_id)

    def _emit(self, team_id: str, message_id: str, **extra) -> None:
        if self.events_hook:
            self.events_hook("a2a.message", {"team_id": team_id, "message_id": message_id, **extra})

    # ---- review --------------------------------------------------------
    def review(self, message_id: str, decision: str, reviewer: str = "owner", note: str = "") -> dict:
        if decision not in DECISIONS:
            raise ValidationError(f"decision must be one of {sorted(DECISIONS)}")
        message = self.get(message_id)
        if message["status"] != "pending":
            raise NotAllowed(
                f"message already {message['status']}", status=message["status"]
            )
        with self.store.tx() as c:
            c.execute(
                """update a2a_messages set status = ?, reviewed_by = ?, review_note = ?,
                       reviewed_date = ? where id = ?""",
                (decision, reviewer, note, now(), message_id),
            )
        return self.get(message_id)

    def review_queue(self, team_id: str, limit: int = 100) -> list[dict]:
        return [
            self._msg(r)
            for r in self.store.query(
                "select * from a2a_messages where team_id = ? and status = 'pending' order by created_date limit ?",
                (team_id, limit),
            )
        ]

    # ---- read ----------------------------------------------------------
    def get(self, message_id: str) -> dict:
        row = self.store.one("select * from a2a_messages where id = ?", (message_id,))
        if not row:
            raise NotFound(f"message {message_id} not found")
        return self._msg(row)

    def inbox(self, agent_id: str, include_sent: bool = False, limit: int = 100) -> list[dict]:
        """approved messages addressed to this agent, plus crew broadcasts.

        pending and rejected messages are deliberately excluded: an unapproved
        message must not reach an agent's context.
        """
        rows = self.store.query(
            """select * from a2a_messages
               where status = 'approved'
                 and (to_agent_id = ? or (to_agent_id is null and team_id = (
                        select team_id from agents where id = ?)))
                 and from_agent_id != ?
               order by created_date desc limit ?""",
            (agent_id, agent_id, agent_id, limit),
        )
        items = [self._msg(r) for r in rows]
        if include_sent:
            sent = self.store.query(
                "select * from a2a_messages where from_agent_id = ? order by created_date desc limit ?",
                (agent_id, limit),
            )
            items += [self._msg(r) for r in sent]
        return items

    def thread(self, thread_id: str) -> list[dict]:
        return [
            self._msg(r)
            for r in self.store.query(
                "select * from a2a_messages where thread_id = ? order by created_date", (thread_id,)
            )
        ]

    def log(self, team_id: str, status: str | None = None, limit: int = 200) -> list[dict]:
        if status:
            rows = self.store.query(
                "select * from a2a_messages where team_id = ? and status = ? order by created_date desc limit ?",
                (team_id, status, limit),
            )
        else:
            rows = self.store.query(
                "select * from a2a_messages where team_id = ? order by created_date desc limit ?",
                (team_id, limit),
            )
        return [self._msg(r) for r in rows]

    def stats(self, team_id: str) -> dict:
        rows = self.store.query(
            "select status, count(*) as n from a2a_messages where team_id = ? group by status",
            (team_id,),
        )
        counts = {r["status"]: r["n"] for r in rows}
        return {
            "pending": counts.get("pending", 0),
            "approved": counts.get("approved", 0),
            "rejected": counts.get("rejected", 0),
            "total": sum(counts.values()),
        }

    def _msg(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "task_id": row["task_id"],
            "from_agent_id": row["from_agent_id"],
            "to_agent_id": row["to_agent_id"],
            "kind": row["kind"],
            "body": row["body"],
            "status": row["status"],
            "reviewed_by": row["reviewed_by"],
            "review_note": row["review_note"],
            "thread_id": row["thread_id"],
            "created_date": row["created_date"],
            "reviewed_date": row["reviewed_date"],
        }


def summarize(messages: list[dict]) -> str:
    """compact context block for an agent prompt."""
    if not messages:
        return ""
    lines = []
    for m in messages:
        lines.append(f"[{m['kind']}] {m['from_agent_id']}: {m['body'][:400]}")
    return "\n".join(lines)
