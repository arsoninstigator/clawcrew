"""self improvement, as a proposal and a promotion, never a silent write.

the failure this exists to avoid: an agent that learns something at 3am and rewrites
its own instructions with it. that is not improvement, it is drift with a good story.
so the loop has three parts and an agent only owns the first:

1. propose. a lesson is a row with the run it came from, so the claim can be checked.
2. approve. a person reads it, and may rewrite the sentence, because the agent is
   usually right about the gap and wrong about the wording.
3. promote. the approved sentence is written into the profile under `instructions`
   with an origin of `lesson:<id>`, so the editor shows where an instruction came from.

the one rule that is not negotiable: a lesson may only write `instructions`. it can
never touch `scope`, `perms` or `tools`, and it can never edit or retire a constraint.
an agent that can loosen its own constraint is not improving, it is escaping.
"""

from __future__ import annotations

from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotAllowed, NotFound, ValidationError

# where a promoted lesson may land. one namespace, on purpose.
TARGET = "instructions"
STATUSES = ("proposed", "approved", "promoted", "rejected")


class LessonService:
    def __init__(self, store: Store, profile=None, crews=None, audit=None):
        self.store = store
        self.profile = profile
        self.crews = crews
        self.audit = audit

    def _row(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "agent_id": row["agent_id"],
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "statement": row["statement"],
            "status": row["status"],
            "write_as": row["write_as"],
            "actor": row["actor"],
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    def get(self, lesson_id: str) -> dict:
        row = self.store.one("select * from lessons where id = ?", (lesson_id,))
        if not row:
            raise NotFound(f"lesson {lesson_id} not found")
        return self._row(row)

    # ---- 1. propose ----------------------------------------------------
    def propose(
        self,
        agent_id: str,
        statement: str,
        run_id: str = "",
        actor: str = "agent",
    ) -> dict:
        statement = " ".join((statement or "").split())
        if len(statement) < 12:
            raise ValidationError("a lesson needs a sentence worth keeping")
        agent = self._agent(agent_id)
        task_id = ""
        if run_id:
            run = self.store.one("select task_id from runs where id = ?", (run_id,))
            if not run:
                raise ValidationError(f"run {run_id} not found")
            task_id = run["task_id"]
        ts = now()
        lesson_id = new_id("les")
        with self.store.tx() as c:
            c.execute(
                """insert into lessons (id, team_id, agent_id, run_id, task_id, statement,
                   status, write_as, actor, created_date, updated_date)
                   values (?,?,?,?,?,?,?,?,?,?,?)""",
                (lesson_id, agent["team_id"], agent_id, run_id, task_id, statement,
                 "proposed", "", actor, ts, ts),
            )
        return self.get(lesson_id)

    def list(self, agent_id: str, status: str | None = None) -> list[dict]:
        sql = "select * from lessons where agent_id = ?"
        params: list = [agent_id]
        if status:
            sql += " and status = ?"
            params.append(status)
        sql += " order by created_date desc"
        return [self._row(r) for r in self.store.query(sql, tuple(params))]

    def candidates(self, team_id: str, limit: int = 10) -> list[dict]:
        """failed runs that never became a lesson. the point of the list is that the
        loop has an empty end: a failure nobody learned from should be findable."""
        return [
            dict(r)
            for r in self.store.query(
                """select r.id as run_id, r.task_id, r.agent_id, r.error, r.created_date
                   from runs r join tasks t on t.id = r.task_id
                   where t.team_id = ? and r.status = 'failed'
                     and not exists (select 1 from lessons l where l.run_id = r.id)
                   order by r.created_date desc limit ?""",
                (team_id, limit),
            )
        ]

    # ---- 2. approve ----------------------------------------------------
    def approve(self, lesson_id: str, statement: str | None = None,
                actor: str = "human") -> dict:
        lesson = self.get(lesson_id)
        if lesson["status"] not in ("proposed", "approved"):
            raise ValidationError(f"lesson is {lesson['status']}, not waiting for a yes")
        final = " ".join((statement or lesson["statement"]).split())
        if len(final) < 12:
            raise ValidationError("an approved lesson needs a sentence worth keeping")
        self._refuse_escape(lesson["agent_id"], final)
        with self.store.tx() as c:
            c.execute(
                "update lessons set statement = ?, status = 'approved', actor = ?,"
                " updated_date = ? where id = ?",
                (final, actor, now(), lesson_id),
            )
        self._audit("lesson.approved", lesson, {"lesson_id": lesson_id}, actor)
        return self.get(lesson_id)

    def reject(self, lesson_id: str, reason: str = "", actor: str = "human") -> dict:
        lesson = self.get(lesson_id)
        if lesson["status"] == "promoted":
            raise ValidationError("a promoted lesson is already in the profile")
        with self.store.tx() as c:
            c.execute(
                "update lessons set status = 'rejected', actor = ?, updated_date = ?"
                " where id = ?",
                (actor, now(), lesson_id),
            )
        self._audit("lesson.rejected", lesson, {"lesson_id": lesson_id,
                                                "reason": reason[:400]}, actor)
        return self.get(lesson_id)

    # ---- 3. promote ----------------------------------------------------
    def promote(self, lesson_id: str, key: str = "", actor: str = "human") -> dict:
        """write an approved lesson into the profile, where the editor can show it.

        `key` names the instruction; the default is `lesson:<id>`, which makes the
        provenance obvious even before the origin column is read.
        """
        lesson = self.get(lesson_id)
        if lesson["status"] != "approved":
            raise ValidationError(
                f"lesson is {lesson['status']}; approve it before promoting it"
            )
        if self.profile is None:
            raise ValidationError("no profile service wired")
        self._refuse_escape(lesson["agent_id"], lesson["statement"])
        entry = key.strip() or f"lesson:{lesson_id}"
        self.profile.set(
            lesson["agent_id"], TARGET, entry, lesson["statement"],
            origin=f"lesson:{lesson_id}",
        )
        with self.store.tx() as c:
            c.execute(
                "update lessons set status = 'promoted', write_as = ?, actor = ?,"
                " updated_date = ? where id = ?",
                (entry, actor, now(), lesson_id),
            )
        self._audit("lesson.promoted", lesson,
                    {"lesson_id": lesson_id, "key": entry}, actor)
        return self.get(lesson_id)

    def _audit(self, event: str, lesson: dict, payload: dict, actor: str) -> None:
        if self.audit is None:
            return
        self.audit.record_event(event, {
            "team_id": lesson["team_id"],
            "agent_id": lesson["agent_id"],
            "actor": actor,
            "data": payload,
        })

    # ---- the rule ------------------------------------------------------
    def _refuse_escape(self, agent_id: str, statement: str) -> None:
        """a lesson that reads like it is loosening a constraint is refused, and the
        refusal is structural, not a request to the agent to please not do that.

        instructions are text, so this is the one place a constraint could be argued
        away in prose. the check is deliberately blunt: quoting a live constraint
        while saying it no longer applies does not get past it.
        """
        if self.crews is None:
            return
        agent = self._agent(agent_id)
        from org.intent import IntentService

        live = IntentService(self.store, crews=self.crews).active(agent["team_id"], agent_id)
        text = statement.lower()
        for intent in live:
            if intent["kind"] != "constraint":
                continue
            words = [w for w in intent["statement"].lower().split() if len(w) > 4]
            hits = sum(1 for w in set(words) if w in text)
            loosening = any(
                marker in text
                for marker in ("no longer", "ignore the", "ignore any", "override", "skip the",
                               "not needed", "does not apply", "bypass")
            )
            if loosening and hits >= 2:
                raise NotAllowed(
                    "a lesson cannot loosen a constraint; constraints are the owner's",
                    constraint=intent["id"],
                )

    def _agent(self, agent_id: str) -> dict:
        if self.crews is not None:
            return self.crews.get_agent(agent_id)
        row = self.store.one("select id, team_id from agents where id = ?", (agent_id,))
        if not row:
            raise NotFound(f"agent {agent_id} not found")
        return {"id": row["id"], "team_id": row["team_id"]}
