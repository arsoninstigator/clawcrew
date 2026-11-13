"""the curated knowledge base: what would take time to explain again.

this is not the document ingestors next door (`manage/knowledge/service.py` pulls from
notion, drive, a folder or an http endpoint). this is the small set of things a crew
writes down because losing them costs an hour: a preference, an id, a decision with its
reason, a process that works, a pattern, a trap.

the useful test is not "is this true" but "would a capable agent already know this from
the task and the repo". that is why `admit` refuses as much as it accepts:

* too short to carry anything ("use postgres")
* a question, which is a task, not knowledge
* a decision with no because, which is an order dressed as a decision
* a failure mode with no symptom, which cannot be recognised later
* a duplicate of a title the crew already has

refusals come back with a reason rather than an exception, because the caller is often
an agent that should be told what to write instead, not simply stopped.
"""

from __future__ import annotations

import re
from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound, ValidationError

# the six that earn their keep, and the ones that do not (in the docstring above).
KINDS = ("preference", "fact", "decision", "process", "pattern", "failure_mode")

MIN_BODY = 24
MIN_TITLE = 4

# words that make an entry expire on its own. a knowledge base full of "right now" is a
# status page, and a status page is worse than nothing because it is believed.
TRANSIENT = ("right now", "currently", "today", "at the moment", "for now", "this week")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def admit(kind: str, title: str, body: str, symptom: str = "",
          workaround: str = "") -> dict:
    """is this worth storing. returns {ok, reason} and never raises, so an agent gets
    told what to fix instead of a 400 with no explanation."""
    if kind not in KINDS:
        return {"ok": False, "reason": f"kind must be one of {list(KINDS)}"}
    raw_body = body or ""
    title, body = _norm(title), _norm(body)
    if len(title) < MIN_TITLE:
        return {"ok": False, "reason": "a title needs to name the thing, at least four characters"}
    if len(body) < MIN_BODY:
        return {"ok": False,
                "reason": f"too short to be worth keeping; {MIN_BODY} characters minimum. "
                          "general knowledge a capable agent already has is the usual cause"}
    if body.rstrip().endswith("?"):
        return {"ok": False, "reason": "a question is a task; record the answer, not the ask"}
    if any(marker in body for marker in TRANSIENT) or any(m in title for m in TRANSIENT):
        return {"ok": False,
                "reason": "sounds transient; write the thing that stays true, or leave it out"}
    if kind == "decision" and "because" not in body:
        return {"ok": False,
                "reason": "a decision needs its because, or the next person reopens it"}
    if kind == "failure_mode" and len(_norm(symptom)) < 8:
        return {"ok": False,
                "reason": "a failure mode needs a symptom, so it can be recognised when it "
                          "happens again"}
    if kind == "process" and "\n" not in raw_body and " then " not in body:
        return {"ok": False,
                "reason": "a process is a sequence; write the steps, one per line"}
    return {"ok": True, "reason": ""}


class KnowledgeBase:
    def __init__(self, store: Store, crews=None, audit=None):
        self.store = store
        self.crews = crews
        self.audit = audit

    def _row(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "agent_id": row["agent_id"],
            "kind": row["kind"],
            "title": row["title"],
            "body": row["body"],
            "symptom": row["symptom"],
            "workaround": row["workaround"],
            "tags": loads(row["tags"], []),
            "source": row["source"],
            "status": row["status"],
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    def get(self, entry_id: str) -> dict:
        row = self.store.one("select * from knowledge_entries where id = ?", (entry_id,))
        if not row:
            raise NotFound(f"knowledge entry {entry_id} not found")
        return self._row(row)

    def record(
        self,
        team_id: str,
        kind: str,
        title: str,
        body: str,
        agent_id: str = "",
        symptom: str = "",
        workaround: str = "",
        tags: list[str] | None = None,
        source: str = "",
        actor: str = "human",
    ) -> dict:
        """store an entry, or say why not.

        the admission test runs here rather than in a route so an ingest path cannot
        skip it, and a duplicate title in the same crew and kind is a refusal: two
        versions of one decision is how a knowledge base starts lying.
        """
        verdict = admit(kind, title, body, symptom=symptom, workaround=workaround)
        if not verdict["ok"]:
            raise ValidationError(verdict["reason"], kind=kind)
        existing = self.store.one(
            "select id from knowledge_entries where team_id = ? and kind = ? and lower(title) = ?",
            (team_id, kind, _norm(title)),
        )
        if existing:
            raise ValidationError(
                "that title is already in the base; update the entry instead of adding a twin",
                entry=existing["id"],
            )
        ts = now()
        entry_id = new_id("kb")
        with self.store.tx() as c:
            c.execute(
                """insert into knowledge_entries (id, team_id, agent_id, kind, title, body,
                   symptom, workaround, tags, source, status, created_date, updated_date)
                   values (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (entry_id, team_id, agent_id, kind, title.strip(), body.strip(), symptom,
                 workaround, dumps(list(tags or [])), source, "active", ts, ts),
            )
        self._audit("knowledge.recorded", team_id, agent_id,
                    {"entry_id": entry_id, "kind": kind}, actor)
        return self.get(entry_id)

    def update(self, entry_id: str, body: str | None = None, tags: list[str] | None = None,
               status: str | None = None, actor: str = "human") -> dict:
        entry = self.get(entry_id)
        fields: dict[str, Any] = {}
        if body is not None:
            verdict = admit(entry["kind"], entry["title"], body, symptom=entry["symptom"],
                            workaround=entry["workaround"])
            if not verdict["ok"]:
                raise ValidationError(verdict["reason"], kind=entry["kind"])
            fields["body"] = body.strip()
        if tags is not None:
            fields["tags"] = dumps(list(tags))
        if status is not None:
            if status not in ("active", "stale", "retired"):
                raise ValidationError("status must be active, stale or retired")
            fields["status"] = status
        if not fields:
            return entry
        fields["updated_date"] = now()
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self.store.tx() as c:
            c.execute(f"update knowledge_entries set {columns} where id = ?",
                      tuple(fields.values()) + (entry_id,))
        self._audit("knowledge.updated", entry["team_id"], entry["agent_id"],
                    {"entry_id": entry_id, "fields": sorted(fields)}, actor)
        return self.get(entry_id)

    def retire(self, entry_id: str, actor: str = "human") -> dict:
        return self.update(entry_id, status="retired", actor=actor)

    def _audit(self, event: str, team_id: str, agent_id: str, payload: dict,
               actor: str) -> None:
        if self.audit is None:
            return
        self.audit.record_event(event, {
            "team_id": team_id,
            "agent_id": agent_id,
            "actor": actor,
            "data": payload,
        })

    # ---- reading it back -----------------------------------------------
    def search(
        self,
        team_id: str,
        query: str = "",
        kind: str = "",
        agent_id: str = "",
        limit: int = 20,
    ) -> list[dict]:
        sql = ("select * from knowledge_entries where team_id = ? and status = 'active'")
        params: list = [team_id]
        if kind:
            sql += " and kind = ?"
            params.append(kind)
        if agent_id:
            sql += " and (agent_id = '' or agent_id = ?)"
            params.append(agent_id)
        sql += " order by updated_date desc limit ?"
        params.append(max(1, limit * 4))
        rows = [self._row(r) for r in self.store.query(sql, tuple(params))]
        if not query:
            return rows[:limit]
        words = [w for w in _norm(query).split() if len(w) > 2]
        if not words:
            return rows[:limit]
        scored = []
        for entry in rows:
            haystack = _norm(" ".join(
                [entry["title"], entry["body"], entry["symptom"], entry["workaround"],
                 " ".join(entry["tags"])]
            ))
            score = sum(1 for w in words if w in haystack)
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda pair: (-pair[0], -pair[1]["updated_date"]))
        return [entry for _score, entry in scored[:limit]]

    def for_prompt(self, team_id: str, agent_id: str = "", limit: int = 6) -> str:
        """the block a run carries. small on purpose: a knowledge dump in every prompt
        is the same failure as no knowledge at all, one context window later."""
        entries = self.search(team_id, agent_id=agent_id, limit=limit)
        if not entries:
            return ""
        lines = ["what this crew already knows (do not re-derive it):"]
        for entry in entries:
            line = f"  [{entry['kind']}] {entry['title']}: {entry['body']}"
            if entry["symptom"]:
                line += f" (looks like: {entry['symptom']})"
            if entry["workaround"]:
                line += f" (works around: {entry['workaround']})"
            lines.append(line[:400])
        return "\n".join(lines)

    def stats(self, team_id: str) -> dict:
        rows = self.store.query(
            "select kind, status, count(*) as n from knowledge_entries where team_id = ?"
            " group by kind, status",
            (team_id,),
        )
        by_kind: dict[str, int] = {}
        for row in rows:
            if row["status"] == "active":
                by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + int(row["n"])
        return {"team_id": team_id, "by_kind": by_kind,
                "total": sum(by_kind.values())}
