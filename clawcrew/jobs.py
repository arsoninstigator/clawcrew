"""celery entrypoint.

the durable queue in sqlite is the source of truth; celery only moves a job id
between processes. that means a celery worker that dies mid-job leaves a `running`
row that can be inspected and requeued, rather than losing the work.

    celery -A clawcrew.jobs worker -Q clawcrew --loglevel=info
"""

from __future__ import annotations

from .app import build_app
from .db import loads, now
from .scheduler import Job

try:  # celery is optional
    from celery import Celery  # type: ignore
except ImportError:  # pragma: no cover
    Celery = None  # type: ignore

app = build_app()
celery = Celery("clawcrew", broker=app.settings.celery_broker_url) if Celery else None
if celery is not None:
    celery.conf.task_default_queue = app.settings.celery_worker_queue


def load_job(job_id: str) -> Job | None:
    row = app.store.one("select * from job_queue where id = ?", (job_id,))
    if not row:
        return None
    return Job(row["id"], row["kind"], loads(row["payload"], {}), row["attempts"])


def run_job(job_id: str) -> dict:
    """celery task: execute one queued job."""
    job = load_job(job_id)
    if job is None:
        return {"job": job_id, "ok": False, "error": "unknown job"}
    try:
        outcome = app.worker._handle(job)
    except Exception as exc:  # noqa: BLE001 - report, do not crash the worker
        app.worker.broker.finish(job_id, False, str(exc))
        return {"job": job_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    app.worker.broker.finish(job_id, True)
    return {"job": job_id, "ok": True, "outcome": outcome, "finished_at": now()}


def tick() -> dict:
    """celery beat: enqueue every schedule that is due. idempotent by design, so a
    beat that fires twice costs one extra query, not one extra run."""
    return {"enqueued": app.scheduler.tick(), "at": now()}


def nightly() -> dict:
    """celery beat, once a night: fire the `schedule.nightly` event, so overnight
    work is a rule an operator can edit instead of a hardcoded cron in a container."""
    return {"fired": app.triggers.fire("schedule.nightly", {"team_id": "", "at": now()})}


def fire_event(event: str, context: dict | None = None) -> dict:
    """celery task: emit an event from outside (a webhook, another system) into the
    same trigger engine the services use."""
    return {"fired": app.triggers.fire(event, context or {})}


def process(limit: int = 10) -> dict:
    return {"results": app.worker.process(limit)}


if celery is not None:
    from celery.schedules import crontab  # type: ignore

    celery.task(name="clawcrew.jobs.run_job")(run_job)
    celery.task(name="clawcrew.jobs.tick")(tick)
    celery.task(name="clawcrew.jobs.nightly")(nightly)
    celery.task(name="clawcrew.jobs.fire_event")(fire_event)
    celery.task(name="clawcrew.jobs.process")(process)
    celery.conf.timezone = "UTC"
    celery.conf.beat_schedule = {
        "clawcrew-tick": {"task": "clawcrew.jobs.tick", "schedule": 15.0},
        "clawcrew-nightly": {
            "task": "clawcrew.jobs.nightly",
            "schedule": crontab(hour=2, minute=0),
        },
    }
    celery.conf.beat_schedule_filename = "/data/celerybeat-schedule"
