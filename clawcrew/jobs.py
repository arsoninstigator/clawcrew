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


if celery is not None:
    celery.task(name="clawcrew.jobs.run_job")(run_job)
