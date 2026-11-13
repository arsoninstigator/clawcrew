"""pov: the whole workspace as one agent sees it.

switching point of view is a *projection*, never a login. nothing is duplicated and
no session is forged: the same rows are read through a different filter, which is why
"view as agent" is honest and cheap. the projection is what makes a fleet legible:
their board, their rooms, their calendar, their inbox, their computer, the shared
context they can see, and only what needs attention.

the calendar is derived, not maintained: explicit blocks an agent owns, plus
scheduled work aimed at them, plus cards carrying a `due_at` in their payload.
a separate calendar table that drifts from reality would be worse than none.
"""

from __future__ import annotations

import time
from typing import Any

from core.db import Store, new_id, now
from core.errors import NotFound, ValidationError

try:  # declared dependency (vendored client uses it too), but stay importable
    from dateutil.parser import isoparse  # type: ignore
except ImportError:  # pragma: no cover
    isoparse = None  # type: ignore

DONE_COLUMNS = {"done", "cancelled", "archived"}


def to_timestamp(value: Any) -> float | None:
    """accept epoch seconds, epoch milliseconds, or an iso8601 string."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    if isinstance(value, str):
        text = value.strip()
        try:
            return to_timestamp(float(text))
        except ValueError:
            pass
        if isoparse is not None:
            try:
                return isoparse(text).timestamp()
            except (ValueError, OverflowError):
                return None
    return None


class PovService:
    def __init__(self, store: Store, kanban, crews, channels=None, departments=None,
                 scheduler=None, profile=None, intent=None, routines=None):
        self.store = store
        self.kanban = kanban
        self.crews = crews
        self.channels = channels
        self.departments = departments
        self.scheduler = scheduler
        self.profile = profile
        self.intent = intent
        self.routines = routines

    # ---- the projection ------------------------------------------------
    def view(self, agent_id: str, days: int = 14) -> dict:
        agent = self.crews.get_agent(agent_id)
        crew = self.crews.get_team(agent["team_id"])
        board = self.board(agent_id)
        calendar = self.calendar(agent_id, days=days)
        inbox = self.inbox(agent_id)
        blocked = [
            task
            for column in board["columns"]
            for task in column["tasks"]
            if column["name"] in {"blocked", "review"}
        ]
        return {
            "as_of": now(),
            "pov": "agent",
            "agent": agent,
            "crew": {k: crew[k] for k in ("id", "name", "slug", "mission", "review_mode")},
            "reporting_line": self.departments.chain(agent_id) if self.departments else [],
            "channels": (
                [
                    {k: c[k] for k in ("id", "name", "slug", "purpose", "kind", "my_role")}
                    for c in self.channels.channels_for_agent(agent_id)
                ]
                if self.channels
                else []
            ),
            "board": board,
            "calendar": calendar,
            "inbox": inbox,
            "computer": self.crews.get_computer(agent_id),
            "intent": self.intent.summary(agent["team_id"], agent_id) if self.intent else None,
            "routines": (
                [
                    {k: r[k] for k in ("id", "name", "cadence", "due_today", "run", "hour")}
                    for r in self.routines.list(agent["team_id"])
                ]
                if self.routines
                else []
            ),
            "context": self.crews.context(agent["team_id"]),
            "runs": self.recent_runs(agent_id),
            "profile": self._profile_bits(agent_id),
            "attention": {
                "needs_you": blocked,
                "pending_reviews": inbox["pending"],
                "due_soon": [entry for entry in calendar["entries"] if entry["kind"] == "task_due"],
            },
        }

    def _profile_bits(self, agent_id: str) -> dict:
        if self.profile is None:
            return {}
        profile = self.profile.profile(agent_id)
        return {
            "fingerprint": profile["fingerprint"],
            "revision": profile["revision"],
            "consistent": profile["consistency"]["ok"],
            "problems": profile["consistency"]["problems"],
            "vertical": (profile["namespaces"].get("vertical") or {}).get("primary"),
            "skills": sorted((profile["namespaces"].get("skills") or {}).keys()),
            "tools": sorted((profile["namespaces"].get("tools") or {}).keys()),
            "grants": [
                {"key": grant["key"], "scopes": grant.get("scopes", [])}
                for grant in self.profile.grants(agent_id)
            ],
            "spawn": self.profile.spawn_policy(agent_id),
            "children": self.profile.children(agent_id),
            "lineage": self.profile.lineage(agent_id),
        }

    # ---- pieces --------------------------------------------------------
    def board(self, agent_id: str) -> dict:
        """their cards, on the same columns as the crew board."""
        agent = self.crews.get_agent(agent_id)
        columns = self.kanban.column_meta(agent["team_id"])
        tasks = self.kanban.list_tasks(
            agent["team_id"], limit=500, assignee_agent_id=agent_id
        )
        return {
            "agent_id": agent_id,
            "columns": [
                {
                    "name": column["name"],
                    "color": column["color"],
                    "hidden": column["hidden"],
                    "tasks": [t for t in tasks if t["column"] == column["name"]],
                }
                for column in columns
            ],
            "open": len([t for t in tasks if t["column"] not in DONE_COLUMNS]),
            "total": len(tasks),
        }

    def calendar(self, agent_id: str, days: int = 14) -> dict:
        agent = self.crews.get_agent(agent_id)
        window_end = now() + max(1, days) * 86400
        entries: list[dict] = []

        for row in self.store.query(
            """select * from agent_events where agent_id = ? and starts_at <= ?
               order by starts_at""",
            (agent_id, window_end),
        ):
            entries.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "title": row["title"],
                    "starts_at": row["starts_at"],
                    "ends_at": row["ends_at"],
                    "source": "agent_event",
                    "ref_id": row["ref_id"],
                }
            )

        for task in self.kanban.list_tasks(agent["team_id"], limit=500, assignee_agent_id=agent_id):
            due = to_timestamp((task.get("payload") or {}).get("due_at"))
            if due is None or due > window_end:
                continue
            entries.append(
                {
                    "id": f"task:{task['id']}",
                    "kind": "task_due",
                    "title": task["title"],
                    "starts_at": due,
                    "ends_at": None,
                    "source": "task",
                    "ref_id": task["id"],
                    "column": task["column"],
                    "channel_id": task.get("channel_id"),
                }
            )

        if self.scheduler is not None:
            for schedule in self.scheduler.list(agent["team_id"]):
                template = schedule.get("task_template") or {}
                if template.get("assignee_agent_id") and template["assignee_agent_id"] != agent_id:
                    continue
                if not schedule.get("enabled") or schedule.get("next_run_at", 0) > window_end:
                    continue
                entries.append(
                    {
                        "id": f"schedule:{schedule['id']}",
                        "kind": "scheduled",
                        "title": template.get("title") or schedule["name"],
                        "starts_at": schedule["next_run_at"],
                        "ends_at": None,
                        "source": "schedule",
                        "ref_id": schedule["id"],
                        "schedule": schedule["kind"],
                    }
                )

        entries.sort(key=lambda entry: entry["starts_at"])
        return {"agent_id": agent_id, "days": days, "entries": entries}

    def add_event(
        self,
        agent_id: str,
        title: str,
        starts_at: Any,
        ends_at: Any = None,
        kind: str = "block",
        ref_id: str = "",
    ) -> dict:
        agent = self.crews.get_agent(agent_id)
        start = to_timestamp(starts_at)
        if start is None:
            raise ValidationError("starts_at must be an epoch timestamp or an iso date")
        end = to_timestamp(ends_at)
        if end is not None and end < start:
            raise ValidationError("ends_at cannot be before starts_at")
        event_id = new_id("aevt")
        with self.store.tx() as c:
            c.execute(
                """insert into agent_events (id, team_id, agent_id, title, kind, starts_at,
                       ends_at, ref_id, created_date)
                   values (?,?,?,?,?,?,?,?,?)""",
                (event_id, agent["team_id"], agent_id, title, kind, start, end, ref_id, now()),
            )
        return {
            "id": event_id,
            "agent_id": agent_id,
            "title": title,
            "kind": kind,
            "starts_at": start,
            "ends_at": end,
            "ref_id": ref_id,
        }

    def delete_event(self, event_id: str, agent_id: str | None = None) -> dict:
        row = self.store.one("select * from agent_events where id = ?", (event_id,))
        if not row:
            raise NotFound(f"calendar event {event_id} not found")
        if agent_id and row["agent_id"] != agent_id:
            raise NotFound(f"calendar event {event_id} does not belong to {agent_id}")
        with self.store.tx() as c:
            c.execute("delete from agent_events where id = ?", (event_id,))
        return {
            "id": row["id"],
            "agent_id": row["agent_id"],
            "title": row["title"],
            "starts_at": row["starts_at"],
        }

    def inbox(self, agent_id: str) -> dict:
        """approved messages to this agent, plus how many are still behind the gate."""
        rows = self.store.query(
            """select * from a2a_messages where to_agent_id = ? and status = 'approved'
               order by created_date desc limit 20""",
            (agent_id,),
        )
        pending = self.store.one(
            """select count(*) as c from a2a_messages
               where to_agent_id = ? and status = 'pending'""",
            (agent_id,),
        )
        return {
            "approved": [
                {
                    "id": r["id"],
                    "from_agent_id": r["from_agent_id"],
                    "task_id": r["task_id"],
                    "kind": r["kind"],
                    "body": r["body"],
                    "thread_id": r["thread_id"],
                    "created_date": r["created_date"],
                }
                for r in rows
            ],
            "pending": int(pending["c"]) if pending else 0,
        }

    def recent_runs(self, agent_id: str, limit: int = 5) -> list[dict]:
        rows = self.store.query(
            """select id, task_id, provider, status, started_at, ended_at, error
               from runs where agent_id = ? order by created_date desc limit ?""",
            (agent_id, limit),
        )
        return [
            {
                "id": r["id"],
                "task_id": r["task_id"],
                "provider": r["provider"],
                "status": r["status"],
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "error": r["error"],
            }
            for r in rows
        ]

    def roster(self, team_id: str) -> list[dict]:
        """every pov in one call: enough to build a switcher without n queries."""
        rows = self.store.query(
            "select id, name, role, status from agents where team_id = ? order by name",
            (team_id,),
        )
        roster = []
        for row in rows:
            open_tasks = self.store.one(
                """select count(*) as c from tasks
                   where assignee_agent_id = ? and column_name not in ('done','cancelled','archived')""",
                (row["id"],),
            )
            roster.append(
                {
                    "agent_id": row["id"],
                    "name": row["name"],
                    "role": row["role"],
                    "status": row["status"],
                    "open_tasks": int(open_tasks["c"]) if open_tasks else 0,
                    "department": (
                        (self.departments.department_for_agent(row["id"]) or {}).get("slug")
                        if self.departments
                        else None
                    ),
                }
            )
        return roster
