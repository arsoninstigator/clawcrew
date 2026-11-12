"""compatibility shim.

the scheduler now lives in `clawcrew/work/schedule/service.py`, next to the job kinds
it dispatches. this module stays because call sites and a deployed schedule entrypoint
already import `clawcrew.scheduler`, and a rename is not worth breaking them. there is
one implementation, and it is not this file.
"""

from ingredients.schedule.service import (  # noqa: F401 - re-exported on purpose
    JOB_KINDS,
    Broker,
    ScheduleBroker,
    CronExpression,
    InProcBroker,
    Job,
    Scheduler,
    Worker,
    make_broker,
)

__all__ = ["JOB_KINDS", "Broker", "ScheduleBroker", "CronExpression", "InProcBroker",
           "Job", "Scheduler", "Worker", "make_broker"]
