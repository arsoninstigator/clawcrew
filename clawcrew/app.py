"""composition root: one object graph, built from settings.

`build_app()` is the only place services know about each other. tests build an app
with a temp db and the deterministic model; production builds one per process and
shares it across requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .a2a import A2AService
from .audit import AuditService
from .channels import ChannelService
from .config import Settings
from .crews import CrewService
from .departments import DepartmentService
from .db import Store
from .errors import ClawCrewError
from .kanban import KanbanService
from .orchestrator import CrewRunner
from .pov import PovService
from .profile import ProfileService
from .runtime import AgentModel
from .sandbox import ProviderRegistry, build_registry
from .scheduler import Scheduler, Worker, make_broker
from .services import ServiceRegistry
from .shifts import ShiftService
from .triggers import TriggerService
from .workflows import WorkflowService


@dataclass
class App:
    settings: Settings
    store: Store
    registry: ProviderRegistry
    crews: CrewService
    kanban: KanbanService
    a2a: A2AService
    runner: CrewRunner
    services: ServiceRegistry
    workflows: WorkflowService
    channels: ChannelService
    departments: DepartmentService
    triggers: TriggerService
    pov: PovService
    audit: AuditService
    shifts: ShiftService
    profile: ProfileService

    def tick(self) -> dict:
        """one beat, both jobs: queue what is due, and put a handoff on the desk of
        whoever just came on shift. called by celery beat, by the cli, and by tests."""
        enqueued = self.scheduler.tick()
        handoffs = self.roll_shifts()
        return {"enqueued": enqueued, "handoffs": [h["id"] for h in handoffs]}

    def roll_shifts(self) -> list[dict]:
        rolled = []
        for row in self.store.query("select id from shift_plans where enabled = 1 order by created_date"):
            try:
                rolled.append(self.shifts.roll(row["id"]))
            except ClawCrewError as exc:
                rolled.append({"plan_id": row["id"], "error": str(exc)})
        return rolled
    scheduler: Scheduler
    worker: Worker

    def counts(self) -> dict[str, int]:
        def n(table: str) -> int:
            row = self.store.one(f"select count(*) as c from {table}")
            return int(row["c"]) if row else 0

        return {
            "crews": n("teams"),
            "agents": n("agents"),
            "computers": n("computers"),
            "tasks": n("tasks"),
            "runs": n("runs"),
            "a2a_messages": n("a2a_messages"),
            "workflows": n("workflows"),
            "workflow_runs": n("workflow_runs"),
            "schedules": n("schedules"),
            "channels": n("channels"),
            "channel_messages": n("channel_messages"),
            "departments": n("departments"),
            "triggers": n("triggers"),
            "shift_plans": n("shift_plans"),
            "handoffs": n("handoffs"),
            "audit_entries": n("agent_audit"),
            "profile_entries": n("agent_profile"),
            "spawns": n("agent_spawns"),
        }


def build_app(settings: Settings | None = None, model: AgentModel | None = None) -> App:
    settings = settings or Settings.from_env()
    store = Store(settings.database_path)
    registry = build_registry(settings)
    crews = CrewService(store, registry)
    kanban = KanbanService(store)
    a2a = A2AService(store)
    runner = CrewRunner(store, registry, settings, model=model)
    services = ServiceRegistry(settings)
    workflows = WorkflowService(store, runner, services=services)
    broker = make_broker(store, settings)
    scheduler = Scheduler(store, broker)
    worker = Worker(store, runner, workflows, broker=broker, scheduler=scheduler)

    # the event graph, wired once. services emit, the trigger engine queues, and
    # nothing has to know who listens.
    triggers = TriggerService(store, broker)
    channels = ChannelService(store, kanban)
    departments = DepartmentService(store, kanban, a2a)
    pov = PovService(store, kanban, crews, channels=channels, departments=departments,
                     scheduler=scheduler)
    audit = AuditService(store)
    shifts = ShiftService(store, crews, kanban, channels=channels, a2a=a2a, audit=audit)
    profile = ProfileService(store, crews=crews, audit=audit)
    crews.profile = profile
    pov.profile = profile
    profile.migrate()
    workflows.scheduler = scheduler
    workflows.audit = audit

    def emit(event: str, context: dict | None = None):
        """one fan-out point: rules queue work, the audit chain records provenance."""
        fired = triggers.fire(event, context)
        audit.record_event(event, context)
        return fired

    kanban.events_hook = emit
    channels.events_hook = emit
    departments.events_hook = emit
    a2a.events_hook = emit
    workflows.events_hook = emit
    shifts.events_hook = emit
    profile.events_hook = emit
    return App(
        settings=settings,
        store=store,
        registry=registry,
        crews=crews,
        kanban=kanban,
        a2a=a2a,
        runner=runner,
        services=services,
        workflows=workflows,
        scheduler=scheduler,
        worker=worker,
        channels=channels,
        departments=departments,
        triggers=triggers,
        pov=pov,
        audit=audit,
        shifts=shifts,
        profile=profile,
    )
