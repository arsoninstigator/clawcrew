"""the night, as numbers: a brief nobody has to argue with.

the point of a brief is that you do not open the board. that only works if the
brief is boringly accurate, so the design is:

* **numbers are computed from rows**, once, and stored with the brief. if someone
  asks next tuesday whether last monday really had two failures, the answer is in
  the row that was shown, not in a recalculation against a db that has moved on.
* **prose is a rendering of the numbers**, not a summary of the world. there is no
  model in this path by default, and if one is ever added it is checked: a brief
  whose prose disagrees with its own counts is stored with the prose dropped.
* **the window is the night**, 18:00 to 06:00 utc by default, because "overnight"
  is a real interval and a rolling 24h hides what happened while you slept.

a brief is per crew, and an agent's brief is the same numbers narrowed to that
agent, not a second computation with its own opinions.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from clawcrew.db import Store, dumps, loads, new_id, now
from clawcrew.errors import NotFound, ValidationError

DAY = 86400.0


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class BriefService:
    def __init__(
        self,
        store: Store,
        crews,
        kanban=None,
        triggers=None,
        workflows=None,
        shifts=None,
        gates=None,
        channels=None,
        window_start_hour: int = 18,
        window_end_hour: int = 6,
        retention_days: int = 90,
    ):
        self.store = store
        self.crews = crews
        self.kanban = kanban
        self.triggers = triggers
        self.workflows = workflows
        self.shifts = shifts
        self.gates = gates
        self.channels = channels
        self.window_start_hour = int(window_start_hour)
        self.window_end_hour = int(window_end_hour)
        self.retention_days = int(retention_days)

    # ---- window --------------------------------------------------------
    def window(self, date: str | None = None) -> tuple[float, float, str]:
        """the night that ends on `date`: 18:00 the day before to 06:00 that day."""
        if date:
            try:
                day = _dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
            except ValueError as exc:
                raise ValidationError("date must be YYYY-MM-DD") from exc
        else:
            day = _dt.datetime.now(_dt.timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0)
        end = day + _dt.timedelta(hours=self.window_end_hour)
        start = day - _dt.timedelta(hours=24 - self.window_start_hour)
        return start.timestamp(), end.timestamp(), day.strftime("%Y-%m-%d")

    # ---- numbers -------------------------------------------------------
    def numbers(self, team_id: str, start: float, end: float, agent_id: str = "") -> dict:
        agent_clause = " and r.agent_id = ?" if agent_id else ""
        agent_params: tuple = (agent_id,) if agent_id else ()

        runs = self.store.query(
            f"""select r.status, count(*) as n from runs r
                join tasks t on t.id = r.task_id
                where t.team_id = ? and r.created_date >= ? and r.created_date <= ?{agent_clause}
                group by r.status""",
            (team_id, start, end) + agent_params)
        by_status = {r["status"]: int(r["n"]) for r in runs}
        total_runs = sum(by_status.values())

        failed_runs = self.store.query(
            f"""select r.id, r.task_id, r.error from runs r
                join tasks t on t.id = r.task_id
                where t.team_id = ? and r.created_date >= ? and r.created_date <= ?
                  and r.status = 'failed'{agent_clause}
                order by r.created_date desc limit 5""",
            (team_id, start, end) + agent_params)

        events = self.store.query(
            """select e.kind, json_extract(e.data, '$.to') as dest, count(*) as n
               from task_events e join tasks t on t.id = e.task_id
               where t.team_id = ? and e.created_date >= ? and e.created_date <= ?
               group by e.kind, dest""",
            (team_id, start, end))
        moved = sum(int(r["n"]) for r in events if r["kind"] == "moved")
        completed = sum(int(r["n"]) for r in events
                        if r["kind"] == "moved" and (r["dest"] or "") == "done")

        fired = 0
        if self.triggers is not None:
            fired = len(self.triggers.log(team_id, start, end))

        waiting = self._waiting(team_id)
        due = self.store.query(
            """select id, title, json_extract(payload, '$.due') as due from tasks
               where team_id = ? and column_name not in ('done', 'archived')
                 and json_extract(payload, '$.due') = ?""",
            (team_id, _dt.datetime.fromtimestamp(end, _dt.timezone.utc).strftime("%Y-%m-%d")))

        numbers = {
            "window": {"from": _iso(start), "to": _iso(end)},
            "overnight": {
                "runs": total_runs,
                "ok": by_status.get("succeeded", 0),
                "failed": by_status.get("failed", 0),
                "running": by_status.get("running", 0) + by_status.get("queued", 0),
                "cards_completed": completed,
                "cards_moved": moved,
                "triggers_fired": fired,
            },
            "waiting": waiting,
            "today": {
                "due": len(due),
                "due_titles": [r["title"] for r in due][:5],
                "on_shift": self._on_shift(team_id),
                "schedules_enabled": self._schedules(team_id),
            },
            "risks": [],
            "failed_runs": [
                {"run_id": r["id"], "task_id": r["task_id"], "error": (r["error"] or "")[:200]}
                for r in failed_runs
            ],
        }
        risks = numbers["risks"]
        if numbers["overnight"]["failed"]:
            risks.append(f"{numbers['overnight']['failed']} run(s) failed overnight")
        for run in numbers["failed_runs"][:3]:
            if run["error"]:
                risks.append(f"{run['task_id']}: {run['error']}")
        if waiting["review"]:
            risks.append(f"{waiting['review']} card(s) sit in review")
        if waiting["gates"]:
            risks.append(f"{waiting['gates']} approval link(s) are unanswered")
        if waiting["handoffs"]:
            risks.append(f"{waiting['handoffs']} handoff(s) not accepted")
        return numbers

    def _waiting(self, team_id: str) -> dict:
        def one(sql: str, params: tuple) -> int:
            row = self.store.one(sql, params)
            return int(row["n"]) if row else 0

        gates = 0
        if self.gates is not None:
            gates = len(self.gates.list(team_id, status="open"))
        waiting_runs = one(
            "select count(*) as n from workflow_runs r join workflows w on w.id = r.workflow_id "
            "where w.team_id = ? and r.status = 'waiting'", (team_id,))
        return {
            "review": one("select count(*) as n from tasks where team_id = ? and column_name = 'review'",
                          (team_id,)),
            "blocked": one("select count(*) as n from tasks where team_id = ? and column_name = 'blocked'",
                           (team_id,)),
            "a2a_pending": one("select count(*) as n from a2a_messages where team_id = ? and status = 'pending'",
                               (team_id,)),
            "handoffs": one("select count(*) as n from handoffs where status = 'open' and to_agent_id in "
                            "(select id from agents where team_id = ?)", (team_id,)),
            "gates": gates,
            "waiting_runs": waiting_runs,
        }

    def _on_shift(self, team_id: str) -> list[str]:
        if self.shifts is None:
            return []
        names: list[str] = []
        try:
            for plan in self.shifts.list_plans(team_id):
                duty = self.shifts.on_duty(plan["id"])
                name = (duty.get("agent") or {}).get("name")
                if name:
                    names.append(name)
        except Exception:  # noqa: BLE001 - a shift plan is not a reason to lose a brief
            return []
        return names

    def _schedules(self, team_id: str) -> int:
        row = self.store.one(
            "select count(*) as n from schedules where team_id = ? and enabled = 1", (team_id,))
        return int(row["n"]) if row else 0

    # ---- prose ---------------------------------------------------------
    def prose(self, numbers: dict, crew_name: str = "the crew") -> str:
        night = numbers["overnight"]
        waiting = numbers["waiting"]
        if not night["runs"] and not night["cards_moved"]:
            return (f"nothing ran overnight for {crew_name}. "
                    f"{waiting['review']} card(s) are still waiting in review.")
        parts = [f"{night['ok']} of {night['runs']} overnight runs finished"]
        if night["failed"]:
            parts.append(f"{night['failed']} failed")
        parts.append(f"{night['cards_completed']} card(s) completed")
        if night["triggers_fired"]:
            parts.append(f"{night['triggers_fired']} rule(s) fired")
        sentence = ", ".join(parts) + "."
        if waiting["gates"]:
            sentence += f" {waiting['gates']} approval link(s) are unanswered."
        elif waiting["review"]:
            sentence += f" {waiting['review']} card(s) need a look."
        return sentence

    def consistent(self, numbers: dict, prose: str) -> bool:
        """prose may say less than the numbers, never something else. the check is
        deliberately crude: every count it names must be a count that exists."""
        if not prose:
            return False
        allowed = set()
        for value in numbers["overnight"].values():
            allowed.add(str(value))
        for value in numbers["waiting"].values():
            allowed.add(str(value))
        for token in prose.replace(".", " ").replace(",", " ").split():
            cleaned = token.strip("()%")
            if cleaned.isdigit() and cleaned not in allowed:
                return False
        return True

    # ---- build and store ----------------------------------------------
    def build(
        self,
        team_id: str,
        date: str | None = None,
        agent_id: str = "",
        store: bool = True,
        include_a2a: bool = False,
    ) -> dict:
        crew = self.crews.get_team(team_id)
        start, end, day = self.window(date)
        numbers = self.numbers(team_id, start, end, agent_id=agent_id)
        prose = self.prose(numbers, crew_name=crew["name"])
        if not self.consistent(numbers, prose):
            prose = ""
        key = f"{day}{'@' + agent_id if agent_id else ''}"
        existing = self.store.one(
            "select id from briefs where team_id = ? and date = ?", (team_id, key)) if store else None
        brief = {
            "id": existing["id"] if existing else new_id("brf"),
            "team_id": team_id,
            "date": day,
            "window": numbers["window"],
            "numbers": numbers,
            "prose": prose,
            "agent_id": agent_id or None,
            "created_date": now(),
        }
        if include_a2a:
            brief["a2a_pending"] = [dict(r) for r in self.store.query(
                "select id, from_agent_id, body from a2a_messages "
                "where team_id = ? and status = 'pending' order by created_date limit 20",
                (team_id,))]
        if store:
            with self.store.tx() as c:
                c.execute(
                    """insert into briefs (id, team_id, date, window_from, window_to, numbers, prose, created_date)
                       values (?,?,?,?,?,?,?,?)
                       on conflict(team_id, date) do update set
                         window_from = excluded.window_from, window_to = excluded.window_to,
                         numbers = excluded.numbers, prose = excluded.prose,
                         created_date = excluded.created_date""",
                    (brief["id"], team_id, key, start, end, dumps(numbers), prose, now()),
                )
            self._prune()
        return brief

    def get(self, team_id: str, date: str, agent_id: str = "") -> dict:
        key = f"{date}{'@' + agent_id if agent_id else ''}"
        row = self.store.one(
            "select * from briefs where team_id = ? and date = ?", (team_id, key))
        if not row:
            raise NotFound(f"no brief for {team_id} on {date}")
        return {
            "id": row["id"], "team_id": row["team_id"], "date": row["date"],
            "window": {"from": _iso(row["window_from"]), "to": _iso(row["window_to"])},
            "numbers": loads(row["numbers"], {}), "prose": row["prose"],
            "created_date": row["created_date"],
        }

    def list(self, team_id: str, limit: int = 14) -> list[dict]:
        rows = self.store.query(
            "select id, date, prose, created_date from briefs where team_id = ? "
            "order by created_date desc limit ?", (team_id, limit))
        return [{"id": r["id"], "date": r["date"], "prose": r["prose"],
                 "created_date": r["created_date"]} for r in rows]

    def digest(self, brief: dict, crew_name: str = "") -> str:
        """one message, readable in a notification, no link required."""
        numbers = brief["numbers"]
        night = numbers["overnight"]
        waiting = numbers["waiting"]
        lines = [
            f"morning brief {brief['date']}{' for ' + crew_name if crew_name else ''}",
            f"overnight: {night['ok']} ok, {night['failed']} failed, "
            f"{night['cards_completed']} cards done, {night['triggers_fired']} rules fired",
            f"waiting on you: {waiting['review']} review, {waiting['gates']} approvals, "
            f"{waiting['handoffs']} handoffs",
        ]
        for risk in numbers["risks"][:3]:
            lines.append(f"- {risk}")
        return "\n".join(lines)

    def _prune(self) -> None:
        cutoff = now() - self.retention_days * DAY
        with self.store.tx() as c:
            c.execute("delete from briefs where created_date < ?", (cutoff,))
