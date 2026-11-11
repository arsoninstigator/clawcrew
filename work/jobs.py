"""compatibility shim.

the celery entrypoint now lives in `clawcrew/work/schedule/jobs.py`, next to the
scheduler it drains. this module stays because a deployed celery worker already
points `-A clawcrew.jobs` at it. there is one implementation, and it is not this file.
"""

from work.schedule.jobs import (  # noqa: F401 - re-exported on purpose
    celery,
    fire_event,
    load_job,
    nightly,
    process,
    run_job,
    tick,
)

__all__ = ["celery", "load_job", "run_job", "tick", "nightly", "fire_event", "process"]
