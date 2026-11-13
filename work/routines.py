"""recurring work that leaves a trace, which is what makes it a routine.

a schedule enqueues a job every fifteen seconds and forgets it. that is the right tool
for a tick and the wrong one for "check the inbox every morning", because a job that
never ran leaves nothing behind. a routine is due on a date, and then either happened
or did not: every day it was due gets a row, and the difference between the two states
is the only report worth reading a week later.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import Conflict, NotFound, ValidationError

CADENCES = ("daily", "weekdays", "weekly", "monthly")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
STATUSES = ("open", "done", "missed")


def day_of(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else now()))


def _as_date(value: str) -> date:
    return date(*time.strptime(value, "%Y-%m-%d")[:3])


def _path_to_done(start: str) -> list[str]:
    """the shortest legal walk from `start` to done, or nothing if there is none."""
    from ingredients.projectmanagement.kanban.board import TRANSITIONS

    if start == "done":
        return []
    queue: list[list[str]] = [[start]]
    seen = {start}
    while queue:
        path = queue.pop(0)
        for nxt in TRANSITIONS.get(path[-1], set()):
            if nxt == "done":
                return path[1:] + ["done"]
            if nxt not in seen:
                seen.add(nxt)
                queue.append(path + [nxt])
    return []


class RoutineService:
    def __init__(self, store: Store, kanban, crews=None):
        self.store = store
        self.kanban = kanban
        self.crews = crews

    # ---- reading -------------------------------------------------------
    def _row(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "agent_id": row["agent_id"],
            "name": row["name"],
            "description": row["description"],
            "cadence": row["cadence"],
            "weekday": int(row["weekday"]),
            "hour": int(row["hour"]),
            "steps": loads(row["steps"], []),
            "required_skills": loads(row["required_skills"], []),
            "status": row["status"],
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    def _run(self, row) -> dict:
        return {
            "id": row["id"],
            "routine_id": row["routine_id"],
            "team_id": row["team_id"],
            "on_date": row["on_date"],
            "status": row["status"],
            "task_id": row["task_id"],
            "notes": row["notes"],
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    def get(self, routine_id: str) -> dict:
        row = self.store.one("select * from routines where id = ?", (routine_id,))
        if not row:
            raise NotFound(f"routine {routine_id} not found")
        return self._row(row)

    def list(self, team_id: str, on: str | None = None, include_paused: bool = False) -> list[dict]:
        """the routines and whether today's has happened yet, which is the only view
        an operator needs at 8am."""
        on_date = on or day_of()
        sql = "select * from routines where team_id = ?"
        if not include_paused:
            sql += " and status = 'active'"
        rows = self.store.query(sql + " order by hour, name", (team_id,))
        out = []
        for row in rows:
            routine = self._row(row)
            run = self.store.one(
                "select * from routine_runs where routine_id = ? and on_date = ?",
                (routine["id"], on_date),
            )
            routine["on_date"] = on_date
            routine["due_today"] = self._is_due(routine, on_date)
            routine["run"] = self._run(run) if run else None
            out.append(routine)
        return out

    def due(self, team_id: str | None = None, on: str | None = None) -> list[dict]:
        on_date = on or day_of()
        if team_id:
            routines = self.list(team_id, on=on_date, include_paused=False)
        else:
            routines = []
            for row in self.store.query(
                "select team_id from routines where status = 'active' group by team_id"
            ):
                routines.extend(self.list(row["team_id"], on=on_date))
        return [r for r in routines if r["due_today"] and not r["run"]]

    def _is_due(self, routine: dict, on_date: str) -> bool:
        day = _as_date(on_date)
        cadence = routine["cadence"]
        if cadence == "daily":
            return True
        if cadence == "weekdays":
            return day.weekday() < 5
        if cadence == "weekly":
            return day.weekday() == max(0, min(6, routine["weekday"]))
        if cadence == "monthly":
            return day.day == 1
        return False

    def run_for(self, routine_id: str, on: str | None = None) -> dict | None:
        row = self.store.one(
            "select * from routine_runs where routine_id = ? and on_date = ?",
            (routine_id, on or day_of()),
        )
        return self._run(row) if row else None

    # ---- writing -------------------------------------------------------
    def create(
        self,
        team_id: str,
        name: str,
        description: str = "",
        cadence: str = "daily",
        agent_id: str = "",
        weekday: str | int = 0,
        hour: int = 8,
        steps: list[str] | None = None,
        required_skills: list[str] | None = None,
    ) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValidationError("a routine needs a name")
        if cadence not in CADENCES:
            raise ValidationError(f"cadence must be one of {list(CADENCES)}")
        if isinstance(weekday, str) and weekday and not weekday.isdigit():
            lowered = weekday.strip().lower()
            if lowered not in WEEKDAYS:
                raise ValidationError(f"weekday must be a number 0-6 or one of {list(WEEKDAYS)}")
            weekday = WEEKDAYS.index(lowered)
        existing = self.store.one(
            "select * from routines where team_id = ? and name = ?", (team_id, name)
        )
        if existing:
            raise Conflict(f"routine '{name}' already exists in this crew")
        if self.crews is not None:
            self.crews.get_team(team_id)
        routine_id = new_id("routine")
        ts = now()
        with self.store.tx() as c:
            c.execute(
                """insert into routines
                   (id, team_id, agent_id, name, description, cadence, weekday, hour,
                    steps, required_skills, status, created_date, updated_date)
                   values (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (routine_id, team_id, agent_id or "", name, description or "", cadence,
                 int(weekday or 0), int(hour or 0), dumps(list(steps or [])),
                 dumps(list(required_skills or [])), "active", ts, ts),
            )
        return self.get(routine_id)

    def pause(self, routine_id: str, status: str = "paused") -> dict:
        if status not in ("active", "paused"):
            raise ValidationError("status must be 'active' or 'paused'")
        self.get(routine_id)
        with self.store.tx() as c:
            c.execute("update routines set status = ?, updated_date = ? where id = ?",
                      (status, now(), routine_id))
        return self.get(routine_id)

    def materialise(self, team_id: str | None = None, on: str | None = None) -> dict:
        """create today's card for every routine due today, once.

        this is the whole difference from a schedule: the card is the promise, and the
        row in `routine_runs` is the receipt. materialising twice in a day is a no-op,
        because both sides of it are keyed on (routine, date).
        """
        on_date = on or day_of()
        swept = self.sweep(team_id, on=on_date)
        created: list[dict] = []
        for routine in self.due(team_id, on=on_date):
            card = self.kanban.create_task(
                routine["team_id"],
                title=f"routine: {routine['name']}",
                description=self._describe(routine),
                column="ready",
                assignee_agent_id=routine["agent_id"] or None,
                required_skills=routine["required_skills"],
                payload={"routine_id": routine["id"], "on_date": on_date},
            )
            run_id = new_id("rrun")
            ts = now()
            with self.store.tx() as c:
                c.execute(
                    """insert into routine_runs
                       (id, routine_id, team_id, on_date, status, task_id, notes,
                        created_date, updated_date)
                       values (?,?,?,?,?,?,?,?,?)""",
                    (run_id, routine["id"], routine["team_id"], on_date, "open",
                     card["id"], "", ts, ts),
                )
            created.append({"routine": routine["name"], "routine_id": routine["id"],
                            "task_id": card["id"], "on_date": on_date})
        return {"on_date": on_date, "created": created, "missed": swept}

    def _describe(self, routine: dict) -> str:
        lines = [routine["description"]] if routine["description"] else []
        if routine["steps"]:
            lines.append("steps, in order:")
            lines.extend(f"  {i}. {step}" for i, step in enumerate(routine["steps"], 1))
        lines.append(f"routine: {routine['name']} ({routine['cadence']})")
        lines.append("when it is done, say so in the last message: the run is the receipt.")
        return "\n".join(lines)

    def sweep(self, team_id: str | None = None, on: str | None = None) -> list[dict]:
        """a routine whose day passed with an open run did not happen. say so once,
        rather than leaving an `open` row that reads like it might still."""
        on_date = on or day_of()
        sql = ("select * from routine_runs where status = 'open' and on_date < ?" +
               (" and team_id = ?" if team_id else ""))
        params: tuple = (on_date, team_id) if team_id else (on_date,)
        missed = []
        ts = now()
        with self.store.tx() as c:
            for row in self.store.query(sql, params):
                c.execute(
                    "update routine_runs set status = 'missed', updated_date = ? where id = ?",
                    (ts, row["id"]),
                )
                missed.append({"routine_id": row["routine_id"], "on_date": row["on_date"]})
        return missed

    def complete(self, routine_id: str, on: str | None = None, notes: str = "",
                 actor: str = "operator") -> dict:
        """the routine happened. close the receipt and, when the card is still there,
        move it to done so the board agrees with the routine."""
        routine = self.get(routine_id)
        on_date = on or day_of()
        run = self.run_for(routine_id, on_date)
        if not run:
            raise NotFound(f"routine '{routine['name']}' was not due on {on_date}")
        if run["status"] == "done":
            return run
        ts = now()
        with self.store.tx() as c:
            c.execute(
                "update routine_runs set status = 'done', notes = ?, updated_date = ?"
                " where id = ?",
                (notes or "", ts, run["id"]),
            )
        if run["task_id"]:
            self._close_card(run["task_id"], actor, notes)
        return self.run_for(routine_id, on_date) or run

    def _close_card(self, task_id: str, actor: str, notes: str) -> None:
        """move the card to done by a path the board allows. a routine has no review
        step to argue about, but the board still owns its transitions, so the routine
        walks them rather than reaching over them."""
        try:
            task = self.kanban.get_task(task_id)
        except NotFound:
            return
        for column in _path_to_done(task["column"]):
            self.kanban.move(task_id, column, actor)

    def summary(self, team_id: str, days: int = 7, on: str | None = None) -> dict:
        """kept against missed, per routine, because a routine that is 80 percent kept
        is a routine that is not worth having and the operator should see that."""
        end = on or day_of()
        start = (date(*time.strptime(end, "%Y-%m-%d")[:3]) - timedelta(days=days - 1)).isoformat()
        rows = self.store.query(
            "select * from routine_runs where team_id = ? and on_date >= ? and on_date <= ?",
            (team_id, start, end),
        )
        per: dict[str, dict] = {}
        for row in rows:
            entry = per.setdefault(row["routine_id"], {"routine_id": row["routine_id"],
                                                       "done": 0, "missed": 0, "open": 0})
            entry[row["status"]] = entry.get(row["status"], 0) + 1
        names = {}
        for row in self.store.query("select id, name from routines where team_id = ?", (team_id,)):
            names[row["id"]] = row["name"]
        items = [{"name": names.get(k, k), **v} for k, v in per.items()]
        kept = sum(i["done"] for i in items)
        due = sum(i["done"] + i["missed"] for i in items)
        return {
            "team_id": team_id,
            "from": start,
            "to": end,
            "days": days,
            "routines": sorted(items, key=lambda i: (i["missed"], i["name"]), reverse=True),
            "kept": kept,
            "missed": sum(i["missed"] for i in items),
            "rate": round(kept / due, 3) if due else None,
        }

    def materialise_all(self, on: str | None = None) -> dict:
        """every crew at once, for the nightly job. one crew raising does not stop the
        rest of the town."""
        on_date = on or day_of()
        done: list[dict] = []
        failed: list[dict] = []
        for row in self.store.query("select id, name from teams"):
            try:
                result = self.materialise(row["id"], on=on_date)
            except Exception as exc:  # a broken routine must not silence the night
                failed.append({"team_id": row["id"], "error": f"{type(exc).__name__}: {exc}"})
                continue
            if result["created"] or result["missed"]:
                done.append({"team_id": row["id"], **result})
        return {"on_date": on_date, "crews": done, "failed": failed}
