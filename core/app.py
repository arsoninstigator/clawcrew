"""composition root: one object graph, built from settings.

`build_app()` is the only place services know about each other. tests build an app
with a temp db and the deterministic model; production builds one per process and
shares it across requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from manage import attest
from ingredients import browser as browser_layer
from comms.a2a import A2AService
from create.coordination import SqliteCoordination
from create.intake import AskService
from ingredients.credentials.credential_broker import CredentialBroker
from manage.approvals.service import GateService
from manage.knowledge.service import ConnectorService
from work.overnight.service import BriefService
from work.ops import build_checks, build_ops
from work.poll.service import PollService
from work.saga.service import SagaService
from manage.audit import AuditService
from comms.channels import ChannelService
from comms.gateway import CommsGateway
from comms.service import CommsService
from core.config import Settings
from agents.crews import CrewService
from org.departments import DepartmentService
from core.db import Store, open_store
from core.errors import CritterTownError
from ingredients.projectmanagement.kanban.board import KanbanService
from agents.orchestrator import CrewRunner
from agents.pov import PovService
from agents.profile import ProfileService
from agents.runtime import AgentModel
from sandbox import ProviderRegistry, build_registry
from work.scheduler import Scheduler, Worker, make_broker
from core.services import ServiceRegistry
from work.shifts import ShiftService
from work.triggers import TriggerService
from work.workflows import WorkflowService


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
    gates: GateService
    brief: BriefService
    connectors: ConnectorService
    vault: CredentialBroker
    asks: AskService
    coordination: SqliteCoordination
    comms: CommsService
    comms_gateway: CommsGateway
    sagas: SagaService
    polls: PollService
    browsers: browser_layer.BrowserService
    ops: Any
    checks: Any

    def tick(self) -> dict:
        """one beat, both jobs: queue what is due, and put a handoff on the desk of
        whoever just came on shift. called by schedule beat, by the cli, and by tests."""
        enqueued = self.scheduler.tick()
        handoffs = self.roll_shifts()
        expired = self.gates.expire()
        return {"enqueued": enqueued, "handoffs": [h["id"] for h in handoffs],
                "gates_expired": expired}

    def roll_shifts(self) -> list[dict]:
        rolled = []
        for row in self.store.query("select id from shift_plans where enabled = 1 order by created_date"):
            try:
                rolled.append(self.shifts.roll(row["id"]))
            except CritterTownError as exc:
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
            "asks": n("asks"),
            "gates": n("gates"),
            "briefs": n("briefs"),
            "connectors": n("connectors"),
            "knowledge_docs": n("knowledge_docs"),
            "vault_grants": n("credential_broker"),
            "comms_events": n("comms_events"),
            "comms_members": n("comms_members"),
        }


def _computer_cdp(crews: CrewService):
    """the cdp endpoint of an agent's own chrome, if its computer publishes one.

    local-browser should drive the browser that is already inside the agent's computer
    rather than start a second one beside it: one profile, one set of logins, one thing
    to close. a computer that does not publish an endpoint returns nothing, and the
    driver launches its own chromium instead.
    """

    def lookup(agent_id: str) -> str:
        computer = crews.get_computer(agent_id)
        if not computer:
            return ""
        handle = computer.get("handle") or {}
        for key in ("cdp_url", "cdp", "browser_cdp_url", "web_socket_url"):
            value = handle.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://", "ws://", "wss://")):
                return value
        return ""

    return lookup


def build_app(settings: Settings | None = None, model: AgentModel | None = None) -> App:
    settings = settings or Settings.from_env()
    # sqlite by default; `CRITTERTOWN_DB_URL` moves the same schema to postgres
    store = open_store(settings)
    registry = build_registry(settings)
    # keys are held here only when this node has custody of them; otherwise rows are
    # written unsigned and say so, rather than pretending to attest.
    signers = attest.KeyStore(store, mint=lambda: settings.comms_custody == "local")
    crews = CrewService(store, registry, keys=signers)
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
    audit = AuditService(store, keys=signers)
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

    # browser access: local-browser drives, container-browser supplies a browser, both local.
    browsers = browser_layer.build_service(store, crews=crews, settings=settings,
                                           computer_cdp=_computer_cdp(crews))
    gates = GateService(store, audit=audit, secret=settings.gate_secret,
                        base_url=settings.public_base_url,
                        ttl_hours=settings.gate_ttl_hours, events_hook=emit)
    gates.workflows = workflows
    gates.channels = channels

    brief = BriefService(store, crews, kanban=kanban, triggers=triggers,
                         workflows=workflows, shifts=shifts, gates=gates,
                         channels=channels,
                         window_start_hour=settings.brief_window_start_hour,
                         window_end_hour=settings.brief_window_end_hour)

    connectors = ConnectorService(store, crews=crews, profile=profile, audit=audit,
                                 events_hook=emit)
    connectors.departments = departments

    asks = AskService(store, crews, kanban, runner, channels=channels,
                      departments=departments, audit=audit, events_hook=emit)
    coordination = SqliteCoordination(store)
    ops = build_ops(SimpleNamespace(kanban=kanban, channels=channels, scheduler=scheduler,
                                   store=store, a2a=a2a))
    checks = build_checks(SimpleNamespace(kanban=kanban, a2a=a2a, store=store))
    sagas = SagaService(store, ops, events_hook=emit)
    polls = PollService(store, checks, broker, events_hook=emit)
    vault = CredentialBroker(store, crews=crews, audit=audit, settings=settings)

    worker.sagas = sagas
    worker.polls = polls
    worker.brief = brief
    worker.shifts = shifts
    worker.gates = gates
    worker.channels = channels
    runner.events_hook = emit
    workflows.gates = gates
    workflows.channels = channels

    kanban.events_hook = emit
    channels.events_hook = emit
    departments.events_hook = emit
    a2a.events_hook = emit
    workflows.events_hook = emit
    shifts.events_hook = emit
    profile.events_hook = emit

    # comms goes last on purpose: it wraps the channel hook that is already set,
    # so a room message is signed after the audit chain has seen it
    comms = CommsService(store, settings=settings, channels=channels)
    comms_gateway = CommsGateway(comms, channels).install()
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
        gates=gates,
        brief=brief,
        connectors=connectors,
        asks=asks,
        coordination=coordination,
        vault=vault,
        comms=comms,
        comms_gateway=comms_gateway,
        sagas=sagas,
        polls=polls,
        browsers=browsers,
        ops=ops,
        checks=checks,
    )
