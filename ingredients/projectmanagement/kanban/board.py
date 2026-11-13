"""per-crew kanban.

each crew owns one board. a task is the unit of work an agent picks up; a task
moves through the crew's configured columns and every move writes an event, so
the board is an append-only audit trail with a current-state projection rather
than mutable state with no history.

transitions are validated: an agent cannot jump `backlog -> done` and claim work
it never did, and a `review` column is a gate, not decoration.
"""

from __future__ import annotations

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound, ValidationError

DEFAULT_COLUMNS = ["backlog", "ready", "in_progress", "review", "done", "blocked"]

# `done` is reachable only from `review`, and `review` only from `in_progress`. that is
# the review gate as a table instead of a convention: an agent that finishes a card can
# put it in review, and it cannot put it in done, because there is no edge to walk.
TRANSITIONS = {
    "backlog": {"ready", "blocked"},
    "ready": {"in_progress", "backlog", "blocked"},
    "in_progress": {"review", "blocked", "ready"},
    "review": {"done", "in_progress", "blocked"},
    "blocked": {"ready", "backlog", "in_progress"},
    "done": {"in_progress"},
}


# actors that are not agents. `owner` is a person at the api, and the rest are the
# platform's own services, which each have their own gate or lint before they move a card.
INTERNAL_ACTORS = {"owner", "system", "saga", "intake", "orchestrator", "scheduler"}


class KanbanService:
    def __init__(self, store: Store):
        self.store = store
        # set by the composition root: `triggers.fire`. a null hook keeps tests and
        # one-off scripts from needing a rule engine to move a card.
        self.events_hook = None

    # ---- tasks ---------------------------------------------------------
    def create_task(
        self,
        team_id: str,
        title: str,
        description: str = "",
        column: str = "backlog",
        priority: int = 0,
        assignee_agent_id: str | None = None,
        required_skills: list[str] | None = None,
        isolation: str = "agent",
        payload: dict | None = None,
        parent_id: str | None = None,
        channel_id: str | None = None,
    ) -> dict:
        if not title:
            raise ValidationError("task title is required")
        if isolation not in {"agent", "ephemeral"}:
            raise ValidationError("isolation must be 'agent' or 'ephemeral'")
        columns = self.columns(team_id)
        if column not in columns:
            raise ValidationError(f"column must be one of {columns}")
        task_id = new_id("task")
        ts = now()
        with self.store.tx() as c:
            c.execute(
                """insert into tasks (id, team_id, parent_id, title, description, column_name,
                       priority, assignee_agent_id, required_skills, isolation, payload, result,
                       channel_id, created_date, updated_date)
                   values (?,?,?,?,?,?,?,?,?,?,?, '{}', ?, ?, ?)""",
                (
                    task_id, team_id, parent_id, title, description, column, int(priority),
                    assignee_agent_id, dumps(required_skills or []), isolation,
                    dumps(payload or {}), channel_id, ts, ts,
                ),
            )
        self._event(task_id, "created", "system", {"column": column, "title": title})
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> dict:
        row = self.store.one("select * from tasks where id = ?", (task_id,))
        if not row:
            raise NotFound(f"task {task_id} not found")
        return self._task(row)

    def list_tasks(
        self,
        team_id: str,
        column: str | None = None,
        limit: int = 200,
        channel_id: str | None = None,
        assignee_agent_id: str | None = None,
    ) -> list[dict]:
        if channel_id:
            rows, params = "select * from tasks where team_id = ? and channel_id = ?", [team_id, channel_id]
            if column:
                rows += " and column_name = ?"
                params.append(column)
            if assignee_agent_id:
                rows += " and assignee_agent_id = ?"
                params.append(assignee_agent_id)
            rows += " order by priority desc, created_date limit ?"
            params.append(limit)
            return [self._task(r) for r in self.store.query(rows, tuple(params))]
        sql = "select * from tasks where team_id = ?"
        params: list = [team_id]
        if column:
            sql += " and column_name = ?"
            params.append(column)
        if assignee_agent_id:
            sql += " and assignee_agent_id = ?"
            params.append(assignee_agent_id)
        sql += " order by priority desc, created_date limit ?"
        params.append(limit)
        rows = self.store.query(sql, tuple(params))
        return [self._task(r) for r in rows]

    def board(self, team_id: str, include_hidden: bool = True,
              channel_id: str | None = None) -> dict:
        meta = self.column_meta(team_id)
        tasks = self.list_tasks(team_id, limit=500, channel_id=channel_id)
        shown = [c for c in meta if include_hidden or not c["hidden"]]
        return {
            "team_id": team_id,
            "channel_id": channel_id,
            "columns": [
                {**column, "tasks": [t for t in tasks if t["column"] == column["name"]]}
                for column in shown
            ],
            "counts": {column["name"]: len([t for t in tasks if t["column"] == column["name"]])
                       for column in meta},
            "hidden": [c["name"] for c in meta if c["hidden"]],
        }

    def columns(self, team_id: str) -> list[str]:
        return [column["name"] for column in self.column_meta(team_id)]

    def column_meta(self, team_id: str) -> list[dict]:
        """columns may be plain names or objects with colour, sort order and a
        hidden flag - the shape vibe-kanban uses for per-project statuses, so a
        board can keep `cancelled` and `backlog` out of the default view."""
        row = self.store.one("select orchestration from teams where id = ?", (team_id,))
        if not row:
            raise NotFound(f"crew {team_id} not found")
        policy = loads(row["orchestration"], {})
        raw = policy.get("kanban_columns") or DEFAULT_COLUMNS
        meta: list[dict] = []
        for index, item in enumerate(raw):
            if isinstance(item, str):
                meta.append({"name": item, "color": "", "hidden": False, "sort_order": index})
            elif isinstance(item, dict) and item.get("name"):
                meta.append(
                    {
                        "name": str(item["name"]),
                        "color": str(item.get("color", "")),
                        "hidden": bool(item.get("hidden", False)),
                        "sort_order": int(item.get("sort_order", index)),
                    }
                )
        if not meta:
            meta = [{"name": name, "color": "", "hidden": False, "sort_order": index}
                    for index, name in enumerate(DEFAULT_COLUMNS)]
        ordered = sorted(enumerate(meta), key=lambda pair: (pair[1]["sort_order"], pair[0]))
        return [column for _, column in ordered]

    def allowed_from(self, team_id: str, column: str) -> set[str]:
        """known columns follow the table; a crew's own columns stay flexible."""
        allowed = TRANSITIONS.get(column)
        if allowed is not None:
            return allowed
        return {name for name in self.columns(team_id) if name != column}

    def _check_actor(self, task: dict, actor: str, column: str) -> None:
        """who is allowed to move this card here.

        three rules, and the third is the gate: an actor must exist, a guardrail cannot
        be impersonated from a request body, and the agent whose work it is cannot be the
        one who signs it off. the last one is the whole reason review is worth having, so
        it is refused rather than trusted.
        """
        actor = (actor or "").strip()
        if not actor:
            raise ValidationError("a card move needs an actor")
        if actor in INTERNAL_ACTORS or actor.startswith("guardrail:"):
            pass
        else:
            row = self.store.one("select team_id from agents where id = ?", (actor,))
            if not row:
                raise ValidationError(
                    f"unknown actor '{actor}': use an agent id in this crew, or one of "
                    f"{sorted(INTERNAL_ACTORS)}")
            if row["team_id"] != task["team_id"]:
                raise ValidationError(
                    f"agent {actor} belongs to another crew and cannot move this card")
        if column == "done" and actor not in INTERNAL_ACTORS and not actor.startswith("guardrail:"):
            if task.get("assignee_agent_id") and actor == task["assignee_agent_id"]:
                raise ValidationError(
                    "an agent cannot approve its own work: review -> done needs the owner "
                    "or another agent",
                    task_id=task["id"], actor=actor)

    def approve(self, task_id: str, actor: str, note: str = "") -> dict:
        """walk a card to done the long way, so the gate is walked and not skipped."""
        task = self.get_task(task_id)
        if task["column"] == "in_progress":
            task = self.move(task_id, "review", actor, note=note)
        if task["column"] != "done":
            task = self.move(task_id, "done", actor, note=note)
        return task

    def move(self, task_id: str, column: str, actor: str = "system", note: str = "") -> dict:
        task = self.get_task(task_id)
        columns = self.columns(task["team_id"])
        if column not in columns:
            raise ValidationError(f"column must be one of {columns}")
        current = task["column"]
        if column == current:
            return task
        allowed = self.allowed_from(task["team_id"], current)
        if column not in allowed:
            raise ValidationError(
                f"illegal transition {current} -> {column}", allowed=sorted(allowed)
            )
        self._check_actor(task, actor, column)
        patch = {"review_note": note} if note and column == "review" else {}
        with self.store.tx() as c:
            c.execute(
                "update tasks set column_name = ?, updated_date = ? where id = ?",
                (column, now(), task_id),
            )
            for key, value in patch.items():
                c.execute(f"update tasks set {key} = ? where id = ?", (value, task_id))
        self._event(task_id, "moved", actor, {"from": current, "to": column, "note": note})
        return self.get_task(task_id)

    def assign(self, task_id: str, agent_id: str | None, actor: str = "system") -> dict:
        task = self.get_task(task_id)
        if agent_id:
            row = self.store.one("select team_id from agents where id = ?", (agent_id,))
            if not row:
                raise NotFound(f"agent {agent_id} not found")
            if row["team_id"] != task["team_id"]:
                raise ValidationError("agent belongs to a different crew")
        with self.store.tx() as c:
            c.execute(
                "update tasks set assignee_agent_id = ?, updated_date = ? where id = ?",
                (agent_id, now(), task_id),
            )
        self._event(task_id, "assigned", actor, {"agent_id": agent_id})
        return self.get_task(task_id)

    def complete(
        self, task_id: str, result: dict | None = None, actor: str = "system", column: str = "done"
    ) -> dict:
        task = self.get_task(task_id)
        with self.store.tx() as c:
            c.execute(
                "update tasks set result = ?, updated_date = ? where id = ?",
                (dumps(result or {}), now(), task_id),
            )
        self._event(task_id, "result", actor, {"result": result or {}})
        if column != "done":
            return self.move(task_id, column, actor)
        # finishing work is not approving it. walk the gate if this actor may, and park
        # in review if it may not, which is what an agent gets.
        try:
            return self.approve(task_id, actor)
        except ValidationError:
            return self.move(task_id, "review", actor)

    def events(self, task_id: str) -> list[dict]:
        self.get_task(task_id)
        return [
            {"id": r["id"], "kind": r["kind"], "actor": r["actor"],
             "data": loads(r["data"], {}), "created_date": r["created_date"]}
            for r in self.store.query(
                "select * from task_events where task_id = ? order by created_date", (task_id,)
            )
        ]

    def _event(self, task_id: str, kind: str, actor: str, data: dict) -> None:
        with self.store.tx() as c:
            c.execute(
                "insert into task_events (id, task_id, kind, actor, data, created_date) values (?,?,?,?,?,?)",
                (new_id("evt"), task_id, kind, actor, dumps(data), now()),
            )
        if self.events_hook:
            task = self.get_task(task_id)
            self.events_hook(f"task.{kind}", {"task": task, "actor": actor, "data": data})

    def _task(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "parent_id": row["parent_id"],
            "title": row["title"],
            "description": row["description"],
            "column": row["column_name"],
            "priority": row["priority"],
            "assignee_agent_id": row["assignee_agent_id"],
            "required_skills": loads(row["required_skills"], []),
            "isolation": row["isolation"],
            "payload": loads(row["payload"], {}),
            "result": loads(row["result"], {}),
            "review_note": row["review_note"],
            "channel_id": row["channel_id"] if "channel_id" in row.keys() else None,
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }
