"""the front door: one sentence in, a staffed plan out.

every entry point before this one is administrative. `POST /crews/{id}/tasks` is a
form; `plan` and `dispatch` are buttons. an operator with an outcome in their head
should not have to fill in a form to get it moving.

so this module does the tearing: it reads a sentence, splits it into the
deliverables it can actually see, places each one in a channel and a department,
picks the agent from the same selector everything else uses, and records the whole
thing as a plan. **plan mode writes nothing to the board you did not ask for**:
cards land in backlog and no run starts. `approve` is the second, one-word step,
which is the summary approval symphony asks for before agents start working.

it is deliberately deterministic: no model in the path. a parser that says the same
thing twice is worth more at 9am than a model that is usually right, and the holes
it leaves are visible (a card with no owner, a clause it could not place) instead
of being smoothed over with fluent text. an llm can be added later behind the same
contract, and the contract is what the tests hold.
"""

from __future__ import annotations

import re
from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import Conflict, NotFound, ValidationError

# clause separators, longest first so "and then" wins over "then"
SPLITTERS = (" and then ", "; ", ";", ", then ", " then ", "\n", " and also ", ", ")
MAX_CARDS = 8

# the words that make an outcome look like work, mapped to skills an agent declares
SKILL_WORDS: dict[str, tuple[str, ...]] = {
    "writing": ("write", "copy", "draft", "post", "blog", "email", "announce", "newsletter"),
    "outreach": ("reach", "contact", "outreach", "follow", "lead", "prospect", "dm"),
    "design": ("design", "logo", "brand", "asset", "banner", "visual"),
    "web": ("page", "website", "site", "deploy", "build", "landing", "app"),
    "finance": ("invoice", "budget", "pay", "cost", "price", "quote", "finance"),
    "scheduling": ("schedule", "book", "calendar", "meeting", "appointment", "reschedule"),
    "research": ("research", "find", "compare", "analyse", "analyze", "survey", "map"),
    "ops": ("fix", "tidy", "clean", "check", "audit", "monitor", "triage"),
}

DATE_WORDS = ("today", "tomorrow", "monday", "tuesday", "wednesday", "thursday",
              "friday", "saturday", "sunday", "next week", "this week", "eod")


class AskService:
    def __init__(self, store: Store, crews, kanban, runner, channels=None,
                 departments=None, audit=None, events_hook=None):
        self.store = store
        self.crews = crews
        self.kanban = kanban
        self.runner = runner
        self.channels = channels
        self.departments = departments
        self.audit = audit
        self.events_hook = events_hook

    # ---- reading the sentence -----------------------------------------
    def clauses(self, text: str) -> list[str]:
        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            return []
        pieces = [cleaned]
        for splitter in SPLITTERS:
            candidate: list[str] = []
            for piece in pieces:
                candidate.extend(piece.split(splitter))
            if len(candidate) > len(pieces):
                pieces = candidate
        pieces = [p.strip(" .") for p in pieces if p and p.strip(" .")]
        # one clause is fine, but a paragraph of them is not a plan
        return pieces[:MAX_CARDS] or [cleaned.strip(" .")]

    def skills_for(self, phrase: str) -> list[str]:
        words = set(re.findall(r"[a-z]+", phrase.lower()))
        skills = [skill for skill, triggers in SKILL_WORDS.items()
                  if words & set(triggers)]
        return skills

    def deadline(self, text: str) -> str:
        lowered = str(text).lower()
        found = re.search(r"\bby ([a-z0-9 ,/-]{3,24})", lowered)
        if found:
            return found.group(1).strip()
        for word in DATE_WORDS:
            if word in lowered:
                return word
        return ""

    def title_for(self, clause: str) -> str:
        title = clause.strip(" .")
        title = re.sub(r"^(please|can you|could you|i need|we need|let's|lets)\s+", "", title,
                       flags=re.I)
        title = title[:80].strip()
        return title[:1].upper() + title[1:] if title else "untitled work"

    # ---- the ask -------------------------------------------------------
    def ask(self, team_id: str, text: str, mode: str = "plan",
            channel_id: str | None = None, due: str = "",
            constraints: dict | None = None, actor: str = "owner",
            idempotency_key: str = "") -> dict:
        if mode not in {"plan", "dispatch"}:
            raise ValidationError("mode must be 'plan' or 'dispatch'")
        crew = self.crews.get_team(team_id)
        if not str(text or "").strip():
            raise ValidationError("ask needs some text to work with")
        if idempotency_key:
            row = self.store.one(
                "select id from asks where team_id = ? and idempotency_key = ?",
                (team_id, idempotency_key))
            if row:
                existing = self.get(row["id"])
                existing["replayed"] = True
                return existing

        clauses = self.clauses(text)
        deadline = due or self.deadline(text)
        channel = channel_id or self._default_channel(team_id)
        agents = [a for a in self.crews.list_agents(team_id) if a["status"] != "retired"]
        available_skills = {s.lower() for agent in agents for s in agent["skills"]}

        plan: list[dict] = []
        questions: list[str] = []
        previous: str | None = None
        for index, clause in enumerate(clauses):
            skills = self.skills_for(clause)
            title = self.title_for(clause)
            done_when = f"finished by {deadline}" if deadline else "reviewed and closed"
            card = self.kanban.create_task(
                team_id=team_id,
                title=title,
                description=f"from the ask: {str(text).strip()}",
                column="backlog",
                priority=max(0, len(clauses) - index),
                required_skills=skills,
                payload={"ask": True, "skills": skills,
                         "depends_on": [previous] if previous else [],
                         "due": deadline or ""},
                channel_id=channel,
            )
            assignee = None
            try:
                assignee = self.runner.pick_agent(team_id, card)
            except ValidationError:
                questions.append(f"no agent available to own '{title}'; add one to the crew")
            department = None
            if assignee is not None:
                if self.departments is not None:
                    dept = self.departments.department_for_agent(assignee["id"])
                    department = dept["name"] if dept else None
                self.kanban.assign(card["id"], assignee["id"], actor="intake")
            entry = {
                "task_id": card["id"],
                "title": card["title"],
                "channel": channel,
                "department": department,
                "assignee": assignee["id"] if assignee else None,
                "assignee_name": assignee["name"] if assignee else None,
                "skills": skills,
                "depends_on": [previous] if previous else [],
                "done_when": done_when,
            }
            plan.append(entry)
            if skills and not (set(skills) & available_skills):
                who = f"assigned to {assignee['name']} by default" if assignee else "unassigned"
                questions.append(
                    f"no agent in {crew['name']} lists {', '.join(skills)}; '{title}' is {who}")
            previous = card["id"]

        interpretation = {
            "goal": self.title_for(clauses[0]) if clauses else "",
            "deliverables": [self.title_for(c) for c in clauses],
            "deadline": deadline,
            "assumptions": [
                f"mode is {mode}",
                "cards land in backlog and nothing runs until approve" if mode == "plan"
                else "cards dispatch as soon as their dependencies are done",
                f"channel is {channel or 'unassigned'}",
            ],
            "questions": questions,
            "constraints": constraints or {},
        }
        ask_id = new_id("ask")
        with self.store.tx() as c:
            c.execute(
                """insert into asks (id, team_id, text, mode, interpretation, plan, status,
                       created_by, idempotency_key, created_date)
                   values (?,?,?,?,?,?,?,?,?,?)""",
                (ask_id, team_id, str(text).strip(), mode, dumps(interpretation), dumps(plan),
                 "planned", actor, idempotency_key, now()),
            )
        self.crews.put_context(team_id, "last_ask", {
            "ask_id": ask_id, "text": str(text).strip(), "mode": mode,
            "plan": [{"task_id": p["task_id"], "title": p["title"]} for p in plan],
            "created_by": actor, "at": now(),
        }, author=f"intake:{actor}")
        dispatched = 0
        if mode == "dispatch":
            dispatched = len(self.dispatch(ask_id)["dispatched"])
        result = self.get(ask_id)
        result["questions"] = questions
        result["dispatched"] = dispatched
        self._emit("ask.created", {"team_id": team_id, "ask": result, "actor": actor})
        return result

    def approve(self, ask_id: str, actor: str = "owner", dispatch: bool = True) -> dict:
        """the one-word second step. a plan nobody approved is just a list."""
        ask = self.get(ask_id)
        if ask["status"] == "cancelled":
            raise Conflict("this ask was cancelled")
        with self.store.tx() as c:
            c.execute("update asks set status = 'approved' where id = ?", (ask_id,))
        result = self.get(ask_id)
        if dispatch:
            result["dispatched"] = self.dispatch(ask_id)["dispatched"]
        self._emit("ask.approved", {"team_id": ask["team_id"], "ask": result, "actor": actor})
        return result

    def dispatch(self, ask_id: str) -> dict:
        """run the ready cards, in order, skipping anything still waiting on a card."""
        ask = self.get(ask_id)
        team_id = ask["team_id"]
        dispatched: list[dict] = []
        skipped: list[dict] = []
        for entry in ask["plan"]:
            card = self.kanban.get_task(entry["task_id"])
            deps = (card.get("payload") or {}).get("depends_on") or []
            unmet = []
            for dep in deps:
                try:
                    parent = self.kanban.get_task(dep)
                except NotFound:
                    continue
                if parent["column"] != "done":
                    unmet.append(dep)
            if unmet:
                skipped.append({"task_id": card["id"], "depends_on": unmet})
                continue
            try:
                if card["column"] == "backlog":
                    card = self.kanban.move(card["id"], "ready", "intake")
                outcome = self.runner.dispatch(card["id"])
            except Exception as exc:  # noqa: BLE001 - one card must not stop the ask
                skipped.append({"task_id": card["id"], "error": f"{type(exc).__name__}: {exc}"})
                continue
            dispatched.append({"task_id": card["id"], "run_id": outcome.get("run_id")})
        with self.store.tx() as c:
            c.execute("update asks set status = 'dispatched' where id = ?", (ask_id,))
        return {"ask_id": ask_id, "team_id": team_id, "dispatched": dispatched,
                "skipped": skipped}

    # ---- reads ---------------------------------------------------------
    def _ask(self, row) -> dict:
        return {
            "id": row["id"], "team_id": row["team_id"], "text": row["text"],
            "mode": row["mode"], "status": row["status"],
            "interpretation": loads(row["interpretation"], {}),
            "plan": loads(row["plan"], []),
            "created_by": row["created_by"], "created_date": row["created_date"],
        }

    def get(self, ask_id: str) -> dict:
        row = self.store.one("select * from asks where id = ?", (ask_id,))
        if not row:
            raise NotFound(f"ask {ask_id} not found")
        ask = self._ask(row)
        ask["questions"] = (ask["interpretation"] or {}).get("questions", [])
        return ask

    def list(self, team_id: str, limit: int = 20) -> list[dict]:
        rows = self.store.query(
            "select * from asks where team_id = ? order by created_date desc limit ?",
            (team_id, limit))
        return [self._ask(r) for r in rows]

    # ---- internals -----------------------------------------------------
    def _default_channel(self, team_id: str) -> str | None:
        if self.channels is None:
            return None
        try:
            channels = self.channels.list(team_id)
        except Exception:  # noqa: BLE001
            return None
        return channels[0]["id"] if channels else None

    def _emit(self, event: str, context: dict) -> None:
        if self.events_hook is not None:
            self.events_hook(event, context)
