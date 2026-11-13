"""what a crew is for, and what would make a finished task the wrong task.

a task says what to do. an intent says why, and what the doing must not break. an
agent handed only a task optimises the task: it publishes the post, or it publishes
three the owner never wanted. this table is the sentence that stops that.

three kinds, because they fail differently:

* `direction` - where the crew is going next. superseded when the operator changes
  their mind, and only one of these is live at a time.
* `constraint` - what must not happen, whatever the task says. these accumulate:
  a crew can have several, and none of them expires on its own.
* `definition_of_done` - what "finished" means here, so a run cannot declare success
  on a number it invented.

an intent is written by a person and proposed by an agent, which is why `actor` is a
column and a proposal needs approving before it can steer anything.
"""

from __future__ import annotations

from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound, ValidationError

KINDS = ("direction", "constraint", "definition_of_done")
STATUSES = ("proposed", "active", "rejected", "superseded")
# a proposal steers nothing. only these are read into a run.
STEERING = ("active",)


class IntentService:
    def __init__(self, store: Store, crews=None):
        self.store = store
        self.crews = crews

    # ---- reading -------------------------------------------------------
    def _row(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "agent_id": row["agent_id"],
            "kind": row["kind"],
            "statement": row["statement"],
            "rationale": row["rationale"],
            "status": row["status"],
            "version": row["version"],
            "supersedes": row["supersedes"],
            "actor": row["actor"],
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    def get(self, intent_id: str) -> dict:
        row = self.store.one("select * from intents where id = ?", (intent_id,))
        if not row:
            raise NotFound(f"intent {intent_id} not found")
        return self._row(row)

    def history(self, team_id: str, agent_id: str | None = None) -> list[dict]:
        """every version, newest first. the point of versioning is that a run from
        last week can be judged against the intent that was live last week."""
        if agent_id is None:
            rows = self.store.query(
                "select * from intents where team_id = ? order by created_date desc",
                (team_id,),
            )
        else:
            rows = self.store.query(
                "select * from intents where team_id = ? and (agent_id = ? or agent_id = '')"
                " order by created_date desc",
                (team_id, agent_id),
            )
        return [self._row(r) for r in rows]

    def active(self, team_id: str, agent_id: str = "") -> list[dict]:
        """what is live for this agent.

        direction and definition_of_done are single valued: an agent's own row replaces
        the crew's for that kind, because two live directions is the failure this table
        exists to prevent. constraints are not single valued. "never publish without a
        link check" and "no outbound email without approval" are both true at once, and
        a crew's constraints still hold for the agent inside it.
        """
        rows = self.store.query(
            "select * from intents where team_id = ? and status = 'active'"
            " and (agent_id = ? or agent_id = '') order by created_date",
            (team_id, agent_id or ""),
        )
        crew: dict[str, list[dict]] = {}
        mine: dict[str, list[dict]] = {}
        for row in rows:
            entry = self._row(row)
            (mine if entry["agent_id"] else crew).setdefault(entry["kind"], []).append(entry)
        merged: list[dict] = []
        for kind in KINDS:
            if kind == "constraint":
                merged.extend(crew.get(kind, []))
                merged.extend(mine.get(kind, []))
            else:
                chosen = mine.get(kind) or crew.get(kind)
                if chosen:
                    merged.append(chosen[-1])
        return merged

    def summary(self, team_id: str, agent_id: str = "") -> dict:
        live = self.active(team_id, agent_id)
        proposals = [
            r for r in self.history(team_id, agent_id or None) if r["status"] == "proposed"
        ]
        return {
            "team_id": team_id,
            "agent_id": agent_id,
            "active": live,
            "proposed": proposals,
            "kinds": {kind: any(i["kind"] == kind for i in live) for kind in KINDS},
        }

    def prompt(self, team_id: str, agent_id: str = "") -> str:
        """the block a run carries. empty when the crew has not said what it is for,
        which is itself worth knowing rather than papering over with a default."""
        live = self.active(team_id, agent_id)
        if not live:
            return ""
        lines = ["the crew's intent, which outranks the task:"]
        for intent in live:
            lines.append(f"  {intent['kind']}: {intent['statement']}")
        return "\n".join(lines)

    # ---- writing -------------------------------------------------------
    def record(
        self,
        team_id: str,
        statement: str,
        kind: str = "direction",
        agent_id: str = "",
        rationale: str = "",
        status: str = "active",
        actor: str = "human",
    ) -> dict:
        """write a new intent. an active one closes the active one of its kind, because
        two live directions is the exact failure this table exists to prevent."""
        statement = (statement or "").strip()
        if not statement:
            raise ValidationError("an intent needs a statement")
        if kind not in KINDS:
            raise ValidationError(f"kind must be one of {list(KINDS)}")
        if status not in STATUSES:
            raise ValidationError(f"status must be one of {list(STATUSES)}")
        if self.crews is not None:
            self.crews.get_team(team_id)
        # a constraint is one more rule, not a new revision of the last one, so it has
        # no previous to close and no version to advance.
        previous = None
        if kind != "constraint":
            previous = self.store.one(
                "select * from intents where team_id = ? and agent_id = ? and kind = ?"
                " and status = 'active' order by version desc",
                (team_id, agent_id or "", kind),
            )
        version = int(previous["version"]) + 1 if previous else 1
        intent_id = new_id("intent")
        ts = now()
        with self.store.tx() as c:
            if previous and status == "active":
                c.execute(
                    "update intents set status = 'superseded', updated_date = ? where id = ?",
                    (ts, previous["id"]),
                )
            c.execute(
                """insert into intents
                   (id, team_id, agent_id, kind, statement, rationale, status, version,
                    supersedes, actor, created_date, updated_date)
                   values (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (intent_id, team_id, agent_id or "", kind, statement, rationale or "",
                 status, version, previous["id"] if previous else "", actor, ts, ts),
            )
        return self.get(intent_id)

    def propose(self, team_id: str, statement: str, kind: str = "direction",
                agent_id: str = "", rationale: str = "") -> dict:
        """an agent's suggestion. it does not steer until a person says so."""
        return self.record(team_id, statement, kind=kind, agent_id=agent_id,
                           rationale=rationale, status="proposed", actor="agent")

    def approve(self, intent_id: str, actor: str = "human", edited: str = "") -> dict:
        """accept a proposal, optionally with the operator's wording, which is the
        normal case: the agent is right about the gap and wrong about the sentence."""
        proposal = self.get(intent_id)
        if proposal["status"] != "proposed":
            raise ValidationError(f"intent {intent_id} is {proposal['status']}, not proposed")
        with self.store.tx() as c:
            c.execute("update intents set status = 'rejected', updated_date = ? where id = ?",
                      (now(), intent_id))
        stored = self.record(
            proposal["team_id"], edited or proposal["statement"], kind=proposal["kind"],
            agent_id=proposal["agent_id"], rationale=proposal["rationale"],
            status="active", actor=actor,
        )
        return {**stored, "approved_from": intent_id}

    def reject(self, intent_id: str, reason: str = "", actor: str = "human") -> dict:
        intent = self.get(intent_id)
        if intent["status"] != "proposed":
            raise ValidationError(f"intent {intent_id} is {intent['status']}, not proposed")
        with self.store.tx() as c:
            c.execute(
                "update intents set status = 'rejected', rationale = ?, actor = ?,"
                " updated_date = ? where id = ?",
                ((reason or intent["rationale"]), actor, now(), intent_id),
            )
        return self.get(intent_id)
