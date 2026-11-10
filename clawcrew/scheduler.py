"""scheduling and the job queue.

three moving parts:

* **schedules** - durable rows (`interval` seconds or a 5-field cron expression).
  `Scheduler.tick()` is idempotent and cheap, so it can be called by a cron
  container, a celery beat task, or the api's own thread.
* **job queue** - a sqlite table used as a durable queue. `claim()` is a single
  `update ... returning`-style statement, so several workers can run against the
  same db without handing a job to two of them.
* **brokers** - `InProcBroker` (default, zero infra) and `CeleryBroker` (hands the
  job to an existing celery cluster, which is how you scale past one worker or
  reuse a zop/celery deployment you already run).

the split matters: the queue is the source of truth, the broker is just how a job
gets picked up. swapping celery in does not change any scheduling semantics.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .db import Store, dumps, loads, new_id, now
from .errors import NotFound, ProviderNotConfigured, ValidationError

JOB_KINDS = {"task", "workflow"}


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


class CeleryBroker(Broker):
    """hands jobs to an existing celery cluster.

    the app is `clawcrew.jobs` and the single task is `run_job`, so a celery
    deployment only needs that module on the path. scheduling semantics stay in
    sqlite either way, celery just moves the work.
    """

    name = "celery"

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
                "celery is not installed; pip install celery or use CLAWCREW_BROKER=inproc"
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
    if settings.broker == "celery":
        return CeleryBroker(store, settings.celery_broker_url, settings.celery_worker_queue)
    return InProcBroker(store)


# ---- worker ------------------------------------------------------------
class Worker:
    """executes queued jobs. one call processes up to `limit` jobs."""

    def __init__(self, store: Store, runner, workflows, broker: Broker | None = None, scheduler: Scheduler | None = None):
        self.store = store
        self.runner = runner
        self.workflows = workflows
        self.broker = broker or InProcBroker(store)
        self.kanban = runner.kanban
        self.crews = runner.crews
        self.scheduler = scheduler

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
        raise ValidationError(f"unknown job kind '{job.kind}'")
