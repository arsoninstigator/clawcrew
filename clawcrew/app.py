"""composition root: one object graph, built from settings.

`build_app()` is the only place services know about each other. tests build an app
with a temp db and the deterministic model; production builds one per process and
shares it across requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .a2a import A2AService
from .config import Settings
from .crews import CrewService
from .db import Store
from .kanban import KanbanService
from .orchestrator import CrewRunner
from .runtime import AgentModel
from .sandbox import ProviderRegistry, build_registry
from .scheduler import Scheduler, Worker, make_broker
from .services import ServiceRegistry
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
    )
