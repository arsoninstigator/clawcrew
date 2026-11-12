"""scheduling and the job queue.

three moving parts:

* **schedules** - durable rows (`interval` seconds or a 5-field cron expression).
  `Scheduler.tick()` is idempotent and cheap, so it can be called by a cron
  container, a schedule beat task, or the api's own thread.
* **job queue** - a sqlite table used as a durable queue. `claim()` is a single
  `update ... returning`-style statement, so several workers can run against the
  same db without handing a job to two of them.
* **brokers** - `InProcBroker` (default, zero infra) and `ScheduleBroker` (hands the
  job to an existing schedule cluster, which is how you scale past one worker or
  reuse a zop/schedule deployment you already run).

the split matters: the queue is the source of truth, the broker is just how a job
gets picked up. swapping schedule in does not change any scheduling semantics.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from core.config import Settings
from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound, ProviderNotConfigured, ValidationError

JOB_KINDS = {"task", "workflow", "saga", "poll"}


# ---- cron --------------------------------------------------------------
class CronExpression:
    """minute hour day-of-month month day-of-week. supports *, */n, a-b, lists."""

    FIELDS = [
        ("minute", 0, 59),
        ("hour", 0, 23),
        ("day of month", 1, 31),
        ("month", 1, 12),
        ("day of week", 0, 6),
    ]

    def __init__(self, spec: str):
        parts = (spec or "").split()
        if len(parts) != 5:
            raise ValidationError("cron spec needs 5 fields: minute hour dom month dow")
        self.spec = " ".join(parts)
        self.sets = []
        for (name, low, high), raw in zip(self.FIELDS, parts):
            self.sets.append(self._parse(raw, name, low, high))

    def _parse(self, raw: str, name: str, low: int, high: int) -> set[int]:
        values: set[int] = set()
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if not all(ch.isdigit() or ch in "*-/," for ch in chunk):
                raise ValidationError(f"bad {name} value in '{raw}'")
            step = 1
            if "/" in chunk:
                chunk, _, step_raw = chunk.partition("/")
                if not step_raw.isdigit() or int(step_raw) == 0:
                    raise ValidationError(f"bad step in {name}: '{raw}'")
                step = int(step_raw)
            if chunk in {"*", ""}:
                start, end = low, high
            elif "-" in chunk:
                a, _, b = chunk.partition("-")
                start, end = int(a), int(b)
            else:
                start = end = int(chunk)
            if start < low or end > high or start > end:
                raise ValidationError(f"{name} out of range in '{raw}'")
            values.update(range(start, end + 1, step))
        if not values:
            raise ValidationError(f"empty {name} field in '{raw}'")
        return values

    def matches(self, ts: float) -> bool:
        t = time.gmtime(ts)
        minute_ok = t.tm_min in self.sets[0]
        hour_ok = t.tm_hour in self.sets[1]
        dom_ok = t.tm_mday in self.sets[2]
        month_ok = t.tm_mon in self.sets[3]
        dow_ok = ((t.tm_wday + 1) % 7) in self.sets[4]  # python monday=0 -> 0=sunday
        return all([minute_ok, hour_ok, dom_ok, month_ok, dow_ok])

    def next_after(self, ts: float, horizon_days: int = 366) -> float:
        candidate = (int(ts) // 60 + 1) * 60
        limit = candidate + horizon_days * 86400
        while candidate <= limit:
            if self.matches(candidate):
                return float(candidate)
            candidate += 60
        raise ValidationError(f"cron '{self.spec}' never fires within {horizon_days} days")


# ---- schedules ---------------------------------------------------------
class Scheduler:
    def __init__(self, store: Store, broker: "Broker | None" = None):
        self.store = store
        self.broker = broker or InProcBroker(store)

    def create(
        self,
        team_id: str,
        name: str,
        kind: str,
        spec: str,
        task_template: dict | None = None,
        start_at: float | None = None,
    ) -> dict:
        if kind not in {"interval", "cron"}:
            raise ValidationError("kind must be 'interval' or 'cron'")
        template = task_template or {}
        if not isinstance(template, dict):
            raise ValidationError("task_template must be an object")
        if template.get("kind") and template["kind"] not in JOB_KINDS:
            raise ValidationError(f"task_template.kind must be one of {sorted(JOB_KINDS)}")
        base = start_at if start_at is not None else now()
        if kind == "interval":
            seconds = int(spec) if str(spec).isdigit() else 0
            if seconds < 30:
                raise ValidationError("interval spec must be at least 30 seconds")
            next_run = base + seconds
        else:
            next_run = CronExpression(spec).next_after(base)
        schedule_id = new_id("sch")
        with self.store.tx() as c:
            c.execute(
                """insert into schedules (id, team_id, name, kind, spec, task_template, enabled,
                       next_run_at, created_date) values (?,?,?,?,?,?,?,?,?)""",
                (schedule_id, team_id, name, kind, str(spec), dumps(template), 1, next_run, now()),
            )
        return self.get(schedule_id)

    def get(self, schedule_id: str) -> dict:
        row = self.store.one("select * from schedules where id = ?", (schedule_id,))
        if not row:
            raise NotFound(f"schedule {schedule_id} not found")
        return {
            "id": row["id"], "team_id": row["team_id"], "name": row["name"], "kind": row["kind"],
            "spec": row["spec"], "task_template": loads(row["task_template"], {}),
            "enabled": bool(row["enabled"]), "next_run_at": row["next_run_at"],
            "last_run_at": row["last_run_at"],
        }

    def list(self, team_id: str) -> list[dict]:
        return [
            self.get(r["id"])
            for r in self.store.query("select id from schedules where team_id = ?", (team_id,))
        ]

    def set_enabled(self, schedule_id: str, enabled: bool) -> dict:
        self.get(schedule_id)
        with self.store.tx() as c:
            c.execute("update schedules set enabled = ? where id = ?", (1 if enabled else 0, schedule_id))
        return self.get(schedule_id)

    def tick(self, at: float | None = None) -> list[str]:
        """enqueue one job per due schedule and advance it. safe to call often."""
        at = now() if at is None else at
        due = self.store.query(
            "select id from schedules where enabled = 1 and next_run_at <= ? order by next_run_at", (at,)
        )
        enqueued: list[str] = []
        for row in due:
            schedule = self.get(row["id"])
            job_id = self.broker.enqueue("schedule", {"schedule_id": schedule["id"], **schedule["task_template"]})
            if schedule["kind"] == "interval":
                next_run = at + int(schedule["spec"])
            else:
                next_run = CronExpression(schedule["spec"]).next_after(at)
            with self.store.tx() as c:
                c.execute(
                    "update schedules set last_run_at = ?, next_run_at = ? where id = ?",
                    (at, next_run, schedule["id"]),
                )
            enqueued.append(job_id)
        return enqueued


# ---- queue -------------------------------------------------------------
@dataclass
class Job:
    id: str
    kind: str
    payload: dict
    attempts: int


class Broker:
    name = "base"

    def enqueue(self, kind: str, payload: dict, delay: float = 0.0) -> str:
        raise NotImplementedError

    def dequeue(self, limit: int = 1) -> list[Job]:
        raise NotImplementedError


class InProcBroker(Broker):
    """durable sqlite queue."""

    name = "inproc"

    def __init__(self, store: Store):
        self.store = store

    def enqueue(self, kind: str, payload: dict, delay: float = 0.0) -> str:
        job_id = new_id("job")
        with self.store.tx() as c:
            c.execute(
                """insert into job_queue (id, kind, payload, status, attempts, available_at, created_date)
                   values (?,?,?, 'pending', 0, ?, ?)""",
                (job_id, kind, dumps(payload), now() + max(0.0, delay), now()),
            )
        return job_id

    def dequeue(self, limit: int = 1) -> list[Job]:
        jobs: list[Job] = []
        for _ in range(limit):
            with self.store.tx() as c:
                row = c.execute(
                    "select * from job_queue where status = 'pending' and available_at <= ? order by created_date limit 1",
                    (now(),),
                ).fetchone()
                if not row:
                    break
                c.execute(
                    "update job_queue set status = 'running', attempts = attempts + 1 where id = ?",
                    (row["id"],),
                )
            jobs.append(Job(row["id"], row["kind"], loads(row["payload"], {}), row["attempts"] + 1))
        return jobs

    def finish(self, job_id: str, ok: bool = True, error: str = "") -> None:
        with self.store.tx() as c:
            c.execute(
                "update job_queue set status = ?, payload = json_set(payload, '$.error', ?) where id = ?",
                ("done" if ok else "failed", error[:500], job_id),
            )

    def stats(self) -> dict:
        rows = self.store.query("select status, count(*) as n from job_queue group by status")
        return {r["status"]: r["n"] for r in rows}


class ScheduleBroker(Broker):
    """hands jobs to an existing broker cluster.

    the app is `ingredients.schedule.jobs` and the single task is `run_job`, so a cluster
    deployment only needs that module on the path. scheduling semantics stay in sqlite
    either way; the broker just moves the work.
    """

    name = "schedule"

    def __init__(self, store: Store, broker_url: str, queue: str = "clawcrew"):
        self.store = store
        self.broker_url = broker_url
        self.queue = queue
        self._fallback = InProcBroker(store)

    def _app(self):
        try:
            from celery import Celery  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ProviderNotConfigured(
                "the broker is not installed; pip install 'clawcrew[schedule]' "
                "or use CLAWCREW_BROKER=inproc",
                provider="schedule",
                hint='pip install "clawcrew[schedule]"',
            ) from exc
        app = Celery("clawcrew", broker=self.broker_url)
        app.conf.task_default_queue = self.queue
        return app

    def enqueue(self, kind: str, payload: dict, delay: float = 0.0) -> str:
        job_id = self._fallback.enqueue(kind, payload, delay)  # durable record always
        app = self._app()
        app.send_task("clawcrew.jobs.run_job", args=[job_id], countdown=max(0, int(delay)))
        return job_id

    def dequeue(self, limit: int = 1) -> list[Job]:
        return self._fallback.dequeue(limit)

    def finish(self, job_id: str, ok: bool = True, error: str = "") -> None:
        self._fallback.finish(job_id, ok, error)

    def stats(self) -> dict:
        return self._fallback.stats()


def make_broker(store: Store, settings: Settings) -> Broker:
    if settings.broker == "schedule":
        return ScheduleBroker(store, settings.schedule_broker_url, settings.schedule_worker_queue)
    return InProcBroker(store)


# ---- worker ------------------------------------------------------------
class Worker:
    """executes queued jobs. one call processes up to `limit` jobs."""

    def __init__(self, store: Store, runner, workflows, broker: Broker | None = None,
                 scheduler: Scheduler | None = None, brief=None, shifts=None,
                 gates=None, channels=None, sagas=None, polls=None):
        self.store = store
        self.runner = runner
        self.workflows = workflows
        self.broker = broker or InProcBroker(store)
        self.kanban = runner.kanban
        self.crews = runner.crews
        self.scheduler = scheduler
        self.brief = brief
        self.shifts = shifts
        self.gates = gates
        self.channels = channels
        self.sagas = sagas
        self.polls = polls

    def process(self, limit: int = 5) -> list[dict]:
        results = []
        for job in self.broker.dequeue(limit):
            try:
                outcome = self._handle(job)
                results.append({"job": job.id, "ok": True, "outcome": outcome})
                if hasattr(self.broker, "finish"):
                    self.broker.finish(job.id, True)
            except Exception as exc:
                results.append({"job": job.id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
                if hasattr(self.broker, "finish"):
                    self.broker.finish(job.id, False, str(exc))
        return results

    def _handle(self, job: Job) -> Any:
        payload = job.payload or {}
        if job.kind == "schedule":
            kind = payload.get("kind") or "task"
            if kind == "workflow":
                workflow_id = payload.get("workflow_id")
                if not workflow_id:
                    raise ValidationError("workflow job needs workflow_id")
                run = self.workflows.run(workflow_id, payload.get("trigger") or {})
                if self.scheduler:
                    with self.store.tx() as c:
                        c.execute(
                            "update schedules set last_run_at = ? where id = ?",
                            (now(), payload.get("schedule_id", "")),
                        )
                return {"workflow_run": run["id"], "status": run["status"]}
            team_id = payload.get("team_id")
            if not team_id:
                raise ValidationError("task job needs team_id")
            task = self.kanban.create_task(
                team_id=team_id,
                title=payload.get("title") or "scheduled task",
                description=payload.get("description", ""),
                column="ready",
                priority=int(payload.get("priority", 0)),
                required_skills=list(payload.get("required_skills") or []),
                isolation=payload.get("isolation", "agent"),
                payload={"scheduled": True, "schedule_id": payload.get("schedule_id")},
            )
            outcome = self.runner.dispatch(task["id"], max_turns=int(payload.get("max_turns", 6)))
            return {"task_id": task["id"], "run_id": outcome["run_id"]}
        if job.kind in {"task", "dispatch"}:
            outcome = self.runner.dispatch(
                payload["task_id"], max_turns=int(payload.get("max_turns", 6))
            )
            return {"run_id": outcome["run_id"]}
        if job.kind == "workflow":
            run = self.workflows.run(payload["workflow_id"], payload.get("trigger") or {})
            return {"workflow_run": run["id"]}
        if job.kind == "brief":
            return self._brief(payload)
        if job.kind == "card":
            return self._card(payload)
        if job.kind == "saga":
            return self._saga(payload)
        if job.kind == "poll":
            return self._poll(payload)
        if job.kind == "shift_roll":
            return self._roll(payload)
        raise ValidationError(f"unknown job kind '{job.kind}'")

    # ---- sequences and waiting ----------------------------------------
    def _saga(self, payload: dict) -> dict:
        """one saga run. step 0 lives here, step 3 may be a rollback, and either way
        the rows say what happened."""
        if self.sagas is None:
            raise ProviderNotConfigured("no saga service wired")
        saga_id = payload.get("saga_id")
        if not saga_id:
            raise ValidationError("saga job needs saga_id")
        saga = self.sagas.run(saga_id)
        return {"saga": saga["id"], "status": saga["status"],
                "steps": [{"name": s["name"], "status": s["status"]} for s in saga["progress"]]}

    def _poll(self, payload: dict) -> dict:
        """one check, then the next attempt is a delayed job, never a sleep."""
        if self.polls is None:
            raise ProviderNotConfigured("no poll service wired")
        poll_id = payload.get("poll_id")
        if not poll_id:
            raise ValidationError("poll job needs poll_id")
        return self.polls.tick(poll_id)

    # ---- the overnight pack's three job kinds --------------------------
    def _brief(self, payload: dict) -> dict:
        """compute the night, store it, and put it where the owner already is."""
        if self.brief is None:
            raise ProviderNotConfigured("no brief service wired")
        team_id = payload.get("team_id")
        if not team_id:
            raise ValidationError("brief job needs team_id")
        brief = self.brief.build(
            team_id,
            date=payload.get("date"),
            include_a2a=bool(payload.get("include_a2a")),
        )
        crew = self.crews.get_team(team_id)
        text = self.brief.digest(brief, crew_name=crew["name"])
        delivered = ""
        if self.channels is not None and not payload.get("dry_run"):
            for channel in self.channels.list(team_id)[:1]:
                self.channels.post(channel["id"], text, author_kind="system")
                delivered = channel["id"]
        return {"brief": brief["id"], "date": brief["date"], "delivered_to": delivered,
                "text": text}

    def _card(self, payload: dict) -> dict:
        """a card without a dispatch: this is how a failure gets a human's eyes
        without starting an agent on top of the failure."""
        team_id = payload.get("team_id")
        if not team_id:
            raise ValidationError("card job needs team_id")
        columns = self.kanban.columns(team_id)
        column = payload.get("column") or "review"
        if column not in columns:
            column = "review" if "review" in columns else columns[0]
        task = self.kanban.create_task(
            team_id=team_id,
            title=str(payload.get("title") or "card")[:160],
            description=str(payload.get("description") or ""),
            column=column,
            priority=int(payload.get("priority", 0)),
            required_skills=list(payload.get("required_skills") or []),
            payload={"from_job": True, "source": payload.get("source", "trigger")},
        )
        return {"task_id": task["id"], "column": task["column"]}

    def _roll(self, payload: dict) -> dict:
        """advance any shift plan whose slot boundary has passed."""
        if self.shifts is None:
            raise ProviderNotConfigured("no shift service wired")
        team_id = payload.get("team_id") or ""
        plans = self.shifts.list_plans(team_id) if team_id else []
        rolled = []
        for plan in plans:
            rolled.append(self.shifts.roll(plan["id"]))
        return {"rolled": len(rolled)}
