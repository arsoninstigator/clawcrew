"""http surface.

one rule: the api holds no logic, it maps http onto the services and maps the
`CritterTownError` ladder onto status codes. anything that needs behaviour belongs in
a service so the worker, the cli and tests all get it.

groups:
  /crews        teams, agents, persistent computers, shared context
  /tasks        kanban board, events, dispatch
  /a2a          message send, review queue, review decisions, inbox, log
  /workflows    dag save/validate/run/resume
  /schedules    interval and cron schedules
  /sagas        a step sequence with defined undos, rolled back in reverse
  /polls        check an external system on an interval until a rule stops it
  /channels     rooms, each with its own board and conversation
  /departments  the org chart, coordinator routing, escalation
  /pov          the workspace as one agent sees it (board, calendar, inbox)
  /triggers     event -> queued action rules
  /shift-plans  coverage rotations, and the handoffs between them
  /audit        the per-agent hash-chained provenance trail
  /ui/pov       the served point-of-view switcher and profile editor
  /ui/isolation the page about two agents drifting into one profile
  /profile      per-agent skills, scope, memory, perms, tools, vertical, spawn policy
  /ops          health, providers, queue tick, worker drain, counts
  /imports      read an agent from another tool's format, and export one back
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from comms import human as a2a_human
from comms import protocol as a2a_spec
from agents.agent_imports import AGENT_SOURCES, adopt, export_agent, import_agent
from core.app import App, build_app
from comms.events import Event
from core.errors import CritterTownError, NotFound, ValidationError
from agents.importers import SOURCES, import_workflow
from manage.knowledge.base import KINDS as KNOWLEDGE_KINDS
from manage.knowledge.service import ADAPTERS
from api.pages import ISOLATION_PAGE, POV_PAGE, gate_page


def create_api(app: App | None = None) -> FastAPI:
    app = app or build_app()
    api = FastAPI(
        title="crittertown",
        version="0.1.0",
        description="agent crews with persistent computers, reviewable a2a, and visual workflows",
    )

    @api.middleware("http")
    async def require_token(request: Request, call_next):
        """single shared token. the backend is meant to sit behind your own auth
        proxy; this is the floor, not a user model."""
        token = app.settings.api_token
        public = request.url.path == "/health" or request.url.path.startswith("/g/")
        if not token or public or request.method == "OPTIONS":
            return await call_next(request)
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        supplied = supplied or request.headers.get("x-crittertown-token", "")
        if supplied != token:
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        return await call_next(request)

    @api.exception_handler(CritterTownError)
    async def _handle(request: Request, exc: CritterTownError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    def body(payload: Any) -> dict:
        if not isinstance(payload, dict):
            raise ValidationError("request body must be a json object")
        return payload

    # ---- ops ----------------------------------------------------------
    @api.get("/health")
    def health() -> dict:
        return {"ok": True, "counts": app.counts(), "providers": app.registry.status()}

    @api.get("/providers")
    def providers() -> dict:
        return {"providers": app.registry.status(), "broker": app.settings.broker}

    @api.get("/services")
    def services() -> dict:
        """the self-hosted tools crittertown plugs into: your n8n, your dify."""
        return {"services": app.services.status()}

    @api.post("/ops/tick")
    def tick() -> dict:
        jobs = app.scheduler.tick()
        return {"enqueued": jobs, "queue": app.worker.broker.stats()}

    @api.post("/ops/process")
    def process(limit: int = Query(10, ge=1, le=100)) -> dict:
        return {"results": app.worker.process(limit)}

    # ---- crews --------------------------------------------------------
    @api.post("/crews")
    def create_crew(payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.crews.create_team(
            name=data.get("name", ""),
            mission=data.get("mission", ""),
            review_mode=data.get("review_mode", app.settings.default_review_mode),
            orchestration=data.get("orchestration"),
        )

    @api.get("/crews")
    def list_crews() -> dict:
        return {"crews": app.crews.list_teams()}

    @api.get("/crews/{team_id}")
    def get_crew(team_id: str) -> dict:
        team = app.crews.get_team(team_id)
        team["agents"] = app.crews.list_agents(team_id)
        team["a2a"] = app.a2a.stats(team_id)
        team["context"] = app.crews.context(team_id)["values"]
        return team

    @api.patch("/crews/{team_id}")
    def update_crew(team_id: str, payload: dict = Body(...)) -> dict:
        return app.crews.update_team(team_id, **body(payload))

    @api.post("/crews/{team_id}/agents")
    def add_agent(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.crews.add_agent(
            team_id=team_id,
            name=data.get("name", ""),
            role=data.get("role", ""),
            model=data.get("model", ""),
            system_prompt=data.get("system_prompt", ""),
            skills=data.get("skills") or [],
            tools=data.get("tools") or [],
            runtime=data.get("runtime"),
            teams=data.get("teams") or [],
            specializations=data.get("specializations") or [],
            verticals=data.get("verticals") or [],
            persistence=data.get("persistence"),
        )

    @api.get("/crews/{team_id}/agents")
    def list_agents(team_id: str, include_members: bool = Query(True)) -> dict:
        return {"agents": app.crews.list_agents(team_id, include_members=include_members)}

    # ---- identity, teams, specializations, persistence -----------------
    @api.get("/agents/{agent_id}/teams")
    def agent_teams(agent_id: str) -> dict:
        return {"agent_id": agent_id, "teams": app.crews.teams_of(agent_id)}

    @api.post("/crews/{team_id}/agents/{agent_id}")
    def join_crew(team_id: str, agent_id: str, payload: dict = Body(default={})) -> dict:
        return app.crews.join_team(agent_id, team_id, role=body(payload or {}).get("role", ""))

    @api.delete("/crews/{team_id}/agents/{agent_id}")
    def leave_crew(team_id: str, agent_id: str) -> dict:
        return app.crews.leave_team(agent_id, team_id)

    @api.post("/agents/{agent_id}/specializations")
    def set_specializations(agent_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.crews.set_specializations(
            agent_id, specializations=data.get("specializations"), verticals=data.get("verticals")
        )

    @api.post("/agents/{agent_id}/persistence")
    def set_persistence(agent_id: str, payload: dict = Body(...)) -> dict:
        return app.crews.set_persistence(agent_id, body(payload).get("mode", ""))

    @api.get("/agents/by-agentid/{agentid}")
    def agent_by_agentid(agentid: str) -> dict:
        return app.crews.by_agentid(agentid)

    @api.get("/agents/{agent_id}/principal")
    def agent_principal(agent_id: str) -> dict:
        """the agent seen from both sides: the key that signs room events and the key
        that signs audit rows, and whether they are still the same one."""
        principal = app.audit.paired(agent_id)
        principal["agent"] = {
            k: app.crews.get_agent(agent_id)[k]
            for k in ("id", "name", "role", "agent_key", "agentid", "teams", "persistence",
                      "specializations", "verticals")
        }
        return principal

    # ---- attestation ---------------------------------------------------
    @api.get("/agents/{agent_id}/attestation")
    def agent_attestation(agent_id: str) -> dict:
        return app.audit.export(agent_id)

    @api.post("/attestations/verify")
    def verify_attestation(payload: dict = Body(...)) -> dict:
        from manage import attest

        bundle = body(payload if isinstance(payload, dict) else {})
        entries = bundle.get("entries") or []
        return attest.verify_entries(entries)

    @api.get("/workflows/{workflow_id}/attestation")
    def workflow_attestation(workflow_id: str) -> dict:
        return app.audit.workflow_attestation(workflow_id)

    # ---- spawning ------------------------------------------------------
    @api.post("/agents/{agent_id}/spawn")
    def spawn_subagent(agent_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        if data.get("lanes"):
            return app.profile.spawn_fan_out(
                agent_id, list(data["lanes"]), task_id=data.get("task_id"),
                role=data.get("role", ""), vertical=data.get("vertical"),
                persistence=data.get("persistence"),
            )
        return app.profile.spawn(
            agent_id, data.get("name", ""), role=data.get("role", ""), mode=data.get("mode"),
            task_id=data.get("task_id"),
            vertical=data.get("vertical"), index=int(data.get("index") or 0),
            specializations=data.get("specializations"),
            persistence=data.get("persistence"),
        )

    @api.post("/agents/{agent_id}/fan-in")
    def fan_in(agent_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.profile.fan_in(agent_id, data.get("task_id", ""), data.get("results") or {})

    # ---- browser -------------------------------------------------------
    @api.get("/browser/providers")
    def browser_providers() -> dict:
        return {"providers": app.browsers.status()}

    @api.post("/agents/{agent_id}/browser")
    def open_browser(agent_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload or {})
        return app.browsers.open(agent_id, data.get("spec") or {}, scope=data.get("scope"),
                                 task_id=data.get("task_id"))

    @api.get("/agents/{agent_id}/browser")
    def list_browsers(agent_id: str, state: str | None = None) -> dict:
        return {"sessions": app.browsers.list(agent_id, state=state)}

    @api.delete("/browser/sessions/{session_id}")
    def close_browser(session_id: str) -> dict:
        return app.browsers.close(session_id)

    @api.post("/agents/{agent_id}/browser/run")
    def run_browser(agent_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.browsers.run(agent_id, data.get("task", ""), spec=data.get("spec") or {},
                                task_id=data.get("task_id"), session_id=data.get("session_id"))

    @api.post("/agents/{agent_id}/computer")
    def ensure_computer(agent_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload or {})
        return app.crews.ensure_computer(agent_id, data.get("spec"))

    @api.get("/agents/{agent_id}/computer")
    def get_computer(agent_id: str) -> dict:
        computer = app.crews.get_computer(agent_id)
        if not computer:
            raise ValidationError("this agent has no computer provisioned yet")
        return computer

    @api.get("/agents/{agent_id}/inbox")
    def inbox(agent_id: str, limit: int = Query(50, ge=1, le=200)) -> dict:
        return {"messages": app.a2a.inbox(agent_id, limit=limit)}

    @api.get("/crews/{team_id}/context")
    def get_context(team_id: str) -> dict:
        return app.crews.context(team_id)

    @api.put("/crews/{team_id}/context")
    def put_context(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.crews.put_context(
            team_id, data.get("key", ""), data.get("value"), author=str(data.get("author", "owner"))
        )

    # ---- channels: rooms with their own board ------------------------
    @api.post("/crews/{team_id}/channels")
    def create_channel(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.channels.create(
            team_id=team_id,
            name=data.get("name", ""),
            purpose=data.get("purpose", ""),
            kind=data.get("kind", "channel"),
            members=data.get("members") or [],
            lead=data.get("lead_agent_id") or data.get("lead"),
        )

    @api.get("/crews/{team_id}/channels")
    def list_channels(team_id: str) -> dict:
        return app.channels.overview(team_id)

    @api.get("/channels/{channel_id}")
    def get_channel(channel_id: str) -> dict:
        return app.channels.get(channel_id)

    @api.patch("/channels/{channel_id}")
    def update_channel(channel_id: str, payload: dict = Body(...)) -> dict:
        return app.channels.update(channel_id, **body(payload))

    @api.post("/channels/{channel_id}/members")
    def join_channel(channel_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        if not data.get("agent_id"):
            raise ValidationError("agent_id is required")
        return app.channels.join(channel_id, data["agent_id"], role=data.get("role", "member"))

    @api.delete("/channels/{channel_id}/members/{agent_id}")
    def leave_channel(channel_id: str, agent_id: str) -> dict:
        return app.channels.leave(channel_id, agent_id)

    @api.post("/channels/{channel_id}/messages")
    def post_message(channel_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.channels.post(
            channel_id,
            data.get("body", ""),
            author_id=data.get("author_id", ""),
            author_kind=data.get("author_kind", "agent"),
            task_id=data.get("task_id"),
        )

    @api.get("/channels/{channel_id}/messages")
    def list_messages(
        channel_id: str,
        limit: int = Query(50, ge=1, le=200),
        task_id: str | None = None,
    ) -> dict:
        return {"messages": app.channels.messages(channel_id, limit=limit, task_id=task_id)}

    @api.get("/channels/{channel_id}/board")
    def channel_board(channel_id: str) -> dict:
        return app.channels.board(channel_id)

    @api.post("/channels/{channel_id}/tasks")
    def channel_task(channel_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        title = data.pop("title", "")
        return app.channels.create_task(channel_id, title, **data)


    # ---- comms: rooms whose members are agents, over a signed log ------
    @api.get("/comms/rooms/{channel_id}")
    def comms_room(channel_id: str) -> dict:
        return {
            "room": app.channels.get(channel_id),
            "stats": app.comms.stats(channel_id),
            "members": app.comms.members(channel_id),
            "bridges": app.comms.bridges(channel_id),
        }

    @api.get("/comms/rooms/{channel_id}/events")
    def comms_events(
        channel_id: str,
        since_seq: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        wire: bool = False,
    ) -> dict:
        """`wire=true` is the raw nip-01 shape, for a client that verifies the
        signature instead of trusting this process."""
        if wire:
            return {"events": app.comms.read_json(channel_id, since_seq=since_seq, limit=limit)}
        return {"events": app.comms.read(channel_id, since_seq=since_seq, limit=limit)}

    @api.post("/comms/rooms/{channel_id}/messages")
    def comms_say(channel_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.comms.say(
            channel_id,
            data.get("body", ""),
            author_id=data.get("author_id") or data.get("agent_id"),
            task_id=data.get("task_id"),
        )

    @api.post("/comms/rooms/{channel_id}/members")
    def comms_join(channel_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        member_id = data.get("member_id") or data.get("agent_id")
        if not member_id:
            raise ValidationError("member_id is required")
        return app.comms.add_member(
            channel_id, member_id, kind=data.get("kind", "agent"),
            role=data.get("role", "member"),
        )

    @api.delete("/comms/rooms/{channel_id}/members/{member_id}")
    def comms_leave(channel_id: str, member_id: str, by: str | None = None) -> dict:
        return app.comms.remove_member(channel_id, member_id, by=by)

    @api.post("/comms/rooms/{channel_id}/bridges")
    def comms_bridge(channel_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.comms.register_bridge(
            channel_id, data.get("transport", ""), data.get("config") or {}
        )

    @api.post("/comms/rooms/{channel_id}/sync")
    def comms_sync(channel_id: str, payload: dict | None = Body(default=None)) -> dict:
        data = body(payload) if payload else {}
        return app.comms.sync(channel_id, data.get("transport"))

    @api.post("/comms/rooms/{channel_id}/subscriptions")
    def comms_subscribe(channel_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.comms.subscribe(
            data.get("name", ""), channel_id, from_start=bool(data.get("from_start"))
        )

    @api.get("/comms/subscriptions/{name}")
    def comms_poll(name: str, limit: int = Query(50, ge=1, le=200), advance: bool = True) -> dict:
        return app.comms.poll(name, limit=limit, advance=advance)

    @api.get("/comms/agents/{agent_id}/identity")
    def comms_identity(agent_id: str) -> dict:
        return app.comms.identity(agent_id)

    @api.post("/comms/agents/{agent_id}/heartbeat")
    def comms_heartbeat(agent_id: str) -> dict:
        return app.comms.heartbeat(agent_id)

    @api.get("/comms/events/{event_id}/verify")
    def comms_verify(event_id: str) -> dict:
        return app.comms.verify(event_id)

    @api.post("/comms/ingest")
    def comms_ingest(payload: dict = Body(...)) -> dict:
        """the inbound half of a bridge: a signed event, verified before it is
        stored, so a bridge cannot forge an author."""
        return app.comms.ingest(Event.from_dict(body(payload)))

    # ---- departments: the org chart -----------------------------------
    @api.post("/crews/{team_id}/departments")
    def create_department(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.departments.create(
            team_id=team_id,
            name=data.get("name", ""),
            coordinator_agent_id=data.get("coordinator_agent_id"),
            parent_id=data.get("parent_id"),
            purpose=data.get("purpose", ""),
            members=data.get("members") or [],
        )

    @api.get("/crews/{team_id}/departments")
    def list_departments(team_id: str) -> dict:
        return {"departments": app.departments.list(team_id)}

    @api.get("/crews/{team_id}/org-chart")
    def org_chart(team_id: str) -> dict:
        return app.departments.tree(team_id)

    @api.patch("/departments/{department_id}")
    def update_department(department_id: str, payload: dict = Body(...)) -> dict:
        return app.departments.update(department_id, **body(payload))

    @api.post("/departments/{department_id}/members")
    def add_department_member(department_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        if not data.get("agent_id"):
            raise ValidationError("agent_id is required")
        return app.departments.add_member(department_id, data["agent_id"])

    @api.post("/tasks/{task_id}/route")
    def route_task(task_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload or {})
        return app.departments.route(task_id, data.get("department_id"))

    @api.post("/agents/{agent_id}/escalate")
    def escalate(agent_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.departments.escalate(
            agent_id, data.get("body", ""), task_id=data.get("task_id")
        )

    # ---- pov: the workspace as one agent sees it ----------------------
    @api.get("/agents/{agent_id}/pov")
    def agent_pov(agent_id: str, days: int = Query(14, ge=1, le=120)) -> dict:
        return app.pov.view(agent_id, days=days)

    @api.get("/agents/{agent_id}/board")
    def agent_board(agent_id: str) -> dict:
        return app.pov.board(agent_id)

    @api.get("/agents/{agent_id}/calendar")
    def agent_calendar(agent_id: str, days: int = Query(14, ge=1, le=120)) -> dict:
        return app.pov.calendar(agent_id, days=days)

    @api.post("/agents/{agent_id}/calendar")
    def add_calendar_event(agent_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.pov.add_event(
            agent_id,
            data.get("title", ""),
            data.get("starts_at"),
            ends_at=data.get("ends_at"),
            kind=data.get("kind", "block"),
            ref_id=data.get("ref_id", ""),
        )

    @api.delete("/agents/{agent_id}/calendar/{event_id}")
    def delete_calendar_event(agent_id: str, event_id: str) -> dict:
        return app.pov.delete_event(event_id, agent_id=agent_id)

    @api.get("/crews/{team_id}/roster")
    def roster(team_id: str) -> dict:
        return {"team_id": team_id, "agents": app.pov.roster(team_id)}

    # ---- triggers: when this happens, queue that ----------------------
    @api.post("/crews/{team_id}/triggers")
    def create_trigger(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.triggers.create(
            team_id=team_id,
            name=data.get("name", ""),
            event=data.get("event", ""),
            action=data.get("action", ""),
            payload=data.get("payload") or {},
            condition=data.get("condition", ""),
            enabled=bool(data.get("enabled", True)),
        )

    @api.get("/crews/{team_id}/triggers")
    def list_triggers(team_id: str) -> dict:
        return {"triggers": app.triggers.list(team_id)}

    @api.post("/triggers/{trigger_id}/enable")
    def enable_trigger(trigger_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload or {})
        return app.triggers.set_enabled(trigger_id, bool(data.get("enabled", True)))

    @api.delete("/triggers/{trigger_id}")
    def delete_trigger(trigger_id: str) -> dict:
        return app.triggers.delete(trigger_id)

    # ---- shifts: coverage and handoffs --------------------------------
    @api.post("/crews/{team_id}/shift-plans")
    def create_shift_plan(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.shifts.create_plan(
            team_id=team_id,
            name=data.get("name", ""),
            rotation=data.get("rotation") or [],
            slot_minutes=int(data.get("slot_minutes", 240)),
            anchor_at=data.get("anchor_at"),
            mode=data.get("mode", "async"),
            channel_id=data.get("channel_id"),
            timezone=data.get("timezone", "UTC"),
            enabled=bool(data.get("enabled", True)),
        )

    @api.get("/crews/{team_id}/shift-plans")
    def list_shift_plans(team_id: str) -> dict:
        return {"plans": app.shifts.list_plans(team_id)}

    @api.get("/crews/{team_id}/coverage")
    def coverage(team_id: str) -> dict:
        return app.shifts.coverage(team_id)

    @api.patch("/shift-plans/{plan_id}")
    def update_shift_plan(plan_id: str, payload: dict = Body(...)) -> dict:
        return app.shifts.update_plan(plan_id, **body(payload))

    @api.delete("/shift-plans/{plan_id}")
    def delete_shift_plan(plan_id: str) -> dict:
        return app.shifts.delete_plan(plan_id)

    @api.get("/shift-plans/{plan_id}/coverage")
    def plan_coverage(plan_id: str) -> dict:
        return app.shifts.on_duty(plan_id)

    @api.get("/shift-plans/{plan_id}/schedule")
    def plan_schedule(plan_id: str, slots: int = Query(8, ge=1, le=96)) -> dict:
        return {"plan_id": plan_id, "slots": app.shifts.schedule(plan_id, slots=slots)}

    @api.post("/shift-plans/{plan_id}/roll")
    def roll_shift(plan_id: str) -> dict:
        return app.shifts.roll(plan_id)

    @api.get("/crews/{team_id}/handoffs")
    def list_handoffs(team_id: str, status: str | None = None,
                      limit: int = Query(50, ge=1, le=200)) -> dict:
        return {"handoffs": app.shifts.handoffs(team_id, status=status, limit=limit)}

    @api.get("/handoffs/{handoff_id}")
    def get_handoff(handoff_id: str) -> dict:
        return app.shifts.get_handoff(handoff_id)

    @api.post("/handoffs/{handoff_id}/accept")
    def accept_handoff(handoff_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload or {})
        return app.shifts.accept(handoff_id, agent_id=data.get("agent_id"), note=data.get("note", ""))

    @api.post("/handoffs/{handoff_id}/decline")
    def decline_handoff(handoff_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload or {})
        return app.shifts.decline(handoff_id, note=data.get("note", ""))

    @api.get("/agents/{agent_id}/briefing")
    def agent_briefing(agent_id: str) -> dict:
        return app.shifts.briefing(agent_id)

    # ---- provenance: the audit chain ----------------------------------
    @api.get("/agents/{agent_id}/audit")
    def agent_audit(agent_id: str, limit: int = Query(100, ge=1, le=500),
                    action: str | None = None) -> dict:
        return {"entries": app.audit.history(agent_id, limit=limit, action=action)}

    @api.get("/agents/{agent_id}/audit/verify")
    def verify_agent_audit(agent_id: str) -> dict:
        return app.audit.verify(agent_id)

    @api.get("/agents/{agent_id}/provenance")
    def agent_provenance(agent_id: str) -> dict:
        summary = app.audit.summary(agent_id)
        summary["agent"] = {
            k: app.crews.get_agent(agent_id)[k]
            for k in ("id", "name", "role", "agent_key", "scope", "skills", "tools")
        }
        return summary

    @api.get("/crews/{team_id}/audit")
    def crew_audit(team_id: str, limit: int = Query(200, ge=1, le=500)) -> dict:
        return {"entries": app.audit.crew_history(team_id, limit=limit)}

    @api.get("/agents/by-key/{agent_key}")
    def agent_by_key(agent_key: str) -> dict:
        return app.crews.by_key(agent_key)

    # ---- a2a read two ways --------------------------------------------
    @api.get("/crews/{team_id}/a2a/transcript")
    def a2a_transcript(team_id: str, limit: int = Query(100, ge=1, le=500),
                       include_pending: bool = True) -> dict:
        return a2a_human.transcript(
            app.a2a, app.crews, team_id, kanban=app.kanban, limit=limit,
            include_pending=include_pending,
        )

    @api.get("/a2a/threads/{thread_id}")
    def a2a_thread(thread_id: str) -> dict:
        return a2a_human.thread_view(app.a2a, app.crews, thread_id, kanban=app.kanban)

    # ---- workflows authored by agents ---------------------------------
    @api.post("/crews/{team_id}/workflows/propose")
    def propose_workflow(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.workflows.propose(
            team_id=team_id,
            name=data.get("name", ""),
            definition=data.get("definition") or {},
            agent_ids=data.get("agent_ids") or [],
            note=data.get("note", ""),
        )

    @api.post("/workflows/{workflow_id}/review")
    def review_workflow(workflow_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.workflows.review(
            workflow_id, data.get("agent_id", ""), data.get("verdict", ""),
            note=data.get("note", ""),
        )

    @api.post("/workflows/{workflow_id}/approve")
    def approve_workflow(workflow_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload or {})
        return app.workflows.approve(
            workflow_id, actor=data.get("actor", "owner"), note=data.get("note", "")
        )

    @api.post("/workflows/{workflow_id}/reject")
    def reject_workflow(workflow_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload or {})
        return app.workflows.reject(
            workflow_id, actor=data.get("actor", "owner"), note=data.get("note", "")
        )

    @api.post("/workflows/{workflow_id}/publish")
    def publish_workflow(workflow_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload or {})
        return app.workflows.publish(
            workflow_id,
            spec=data.get("spec", ""),
            kind=data.get("kind", "cron"),
            schedule_name=data.get("schedule_name"),
        )

    # ---- the served pov switcher --------------------------------------
    @api.get("/ui/pov", response_class=HTMLResponse)
    def pov_page() -> str:
        return POV_PAGE

    @api.get("/ui/isolation", response_class=HTMLResponse)
    def isolation_page() -> str:
        """the one canvas page about a failure rather than a view: two agents that
        drifted into the same profile, shown as a page and not a line in a report."""
        return ISOLATION_PAGE

    # ---- per-agent profile: nine namespaces, no shared bucket ----------
    @api.get("/agents/{agent_id}")
    def get_agent(agent_id: str) -> dict:
        agent = app.crews.get_agent(agent_id)
        agent["profile"] = app.profile.profile(agent_id)
        return agent

    @api.get("/agents/{agent_id}/profile")
    def agent_profile(agent_id: str) -> dict:
        return app.profile.profile(agent_id)

    @api.put("/agents/{agent_id}/profile/{namespace}/{key}")
    def set_profile_entry(agent_id: str, namespace: str, key: str,
                          payload: dict = Body(...)) -> dict:
        data = body(payload)
        if "value" not in data:
            raise ValidationError("body needs a 'value'")
        return app.profile.set(
            agent_id, namespace, key, data["value"], origin=data.get("origin", "agent")
        )

    @api.delete("/agents/{agent_id}/profile/{namespace}/{key}")
    def delete_profile_entry(agent_id: str, namespace: str, key: str) -> dict:
        return app.profile.delete(agent_id, namespace, key)

    @api.get("/agents/{agent_id}/effective")
    def agent_effective(agent_id: str) -> dict:
        team = app.crews.get_agent(agent_id)["team_id"]
        defaults = (app.crews.get_team(team)["orchestration"] or {}).get("agent_defaults") or {}
        return app.profile.effective(agent_id, defaults=defaults)

    @api.get("/agents/{agent_id}/consistency")
    def agent_consistency(agent_id: str) -> dict:
        return app.profile.consistent(agent_id)

    @api.get("/agents/{agent_id}/memory")
    def agent_memory(agent_id: str) -> dict:
        return app.profile.memory(agent_id)

    @api.post("/agents/{agent_id}/memory")
    def remember(agent_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        if not data.get("key"):
            raise ValidationError("key is required")
        return app.profile.remember(
            agent_id, data["key"], data.get("value"), kind=data.get("kind", "note")
        )

    @api.delete("/agents/{agent_id}/memory/{key}")
    def forget(agent_id: str, key: str) -> dict:
        return app.profile.forget(agent_id, key)

    @api.get("/agents/{agent_id}/grants")
    def agent_grants(agent_id: str, active_only: bool = True) -> dict:
        return {"grants": app.profile.grants(agent_id, active_only=active_only)}

    @api.post("/agents/{agent_id}/grants")
    def add_grant(agent_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.profile.grant(
            agent_id,
            kind=data.get("kind", ""),
            resource=data.get("resource", ""),
            scopes=data.get("scopes") or [],
            expires_at=data.get("expires_at"),
            granted_by=data.get("granted_by", "owner"),
        )

    @api.delete("/agents/{agent_id}/grants/{kind}/{resource}")
    def revoke_grant(agent_id: str, kind: str, resource: str) -> dict:
        return app.profile.revoke(agent_id, kind, resource)

    @api.get("/agents/{agent_id}/spawn-policy")
    def spawn_policy(agent_id: str) -> dict:
        return app.profile.spawn_policy(agent_id)

    @api.put("/agents/{agent_id}/spawn-policy")
    def set_spawn_policy(agent_id: str, payload: dict = Body(...)) -> dict:
        return app.profile.set_spawn_policy(agent_id, **body(payload))

    @api.get("/agents/{agent_id}/children")
    def agent_children(agent_id: str, include_retired: bool = False) -> dict:
        return {"children": app.profile.children(agent_id, include_retired=include_retired)}

    @api.get("/agents/{agent_id}/lineage")
    def agent_lineage(agent_id: str) -> dict:
        return {"lineage": app.profile.lineage(agent_id)}

    @api.post("/agents/{agent_id}/children/{child_agent_id}/retire")
    def retire_child(agent_id: str, child_agent_id: str,
                     payload: dict = Body(default={})) -> dict:
        data = body(payload or {})
        child = app.crews.get_agent(child_agent_id)
        if not any(c["child_agent_id"] == child["id"] for c in app.profile.children(agent_id, include_retired=True)):
            raise ValidationError("that agent is not a subagent of this one")
        return app.profile.retire(child_agent_id, reason=data.get("reason", ""))

    @api.get("/crews/{team_id}/isolation")
    def isolation_report(team_id: str) -> dict:
        return app.profile.isolation_report(team_id)

    # ---- kanban -------------------------------------------------------
    @api.post("/crews/{team_id}/tasks")
    def create_task(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.kanban.create_task(
            team_id=team_id,
            title=data.get("title", ""),
            description=data.get("description", ""),
            column=data.get("column", "backlog"),
            priority=int(data.get("priority", 0)),
            assignee_agent_id=data.get("assignee_agent_id"),
            required_skills=data.get("required_skills") or [],
            isolation=data.get("isolation", "agent"),
            payload=data.get("payload") or {},
            parent_id=data.get("parent_id"),
        )

    @api.get("/crews/{team_id}/tasks")
    def list_tasks(team_id: str, column: str | None = None) -> dict:
        return {"tasks": app.kanban.list_tasks(team_id, column)}

    @api.get("/crews/{team_id}/board")
    def board(team_id: str, include_hidden: bool = True) -> dict:
        return app.kanban.board(team_id, include_hidden=include_hidden)

    @api.get("/tasks/{task_id}")
    def get_task(task_id: str) -> dict:
        task = app.kanban.get_task(task_id)
        task["events"] = app.kanban.events(task_id)
        task["runs"] = app.runner.list_runs(task_id)
        return task

    @api.post("/tasks/{task_id}/move")
    def move_task(task_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.kanban.move(
            task_id, data.get("column", ""), actor=str(data.get("actor", "owner")), note=data.get("note", "")
        )

    @api.post("/tasks/{task_id}/assign")
    def assign_task(task_id: str, payload: dict = Body(...)) -> dict:
        return app.kanban.assign(task_id, body(payload).get("agent_id"))

    @api.post("/tasks/{task_id}/plan")
    def plan_task(task_id: str) -> dict:
        return {"subtasks": app.runner.plan(task_id)}

    @api.post("/tasks/{task_id}/dispatch")
    def dispatch_task(task_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload or {})
        return app.runner.dispatch(task_id, max_turns=int(data.get("max_turns", 6)))

    @api.get("/tasks/{task_id}/events")
    def task_events(task_id: str) -> dict:
        return {"events": app.kanban.events(task_id)}

    @api.get("/tasks/{task_id}/runs")
    def task_runs(task_id: str) -> dict:
        return {"runs": app.runner.list_runs(task_id)}

    @api.post("/crews/{team_id}/dispatch")
    def dispatch_ready(team_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload or {})
        return {
            "outcomes": app.runner.dispatch_ready(
                team_id, limit=int(data.get("limit", 10)), max_turns=int(data.get("max_turns", 6))
            )
        }

    @api.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        return app.runner.get_run(run_id)

    # ---- a2a ----------------------------------------------------------
    @api.post("/a2a")
    def send_message(payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.a2a.send(
            team_id=data.get("team_id", ""),
            from_agent_id=data.get("from_agent_id", ""),
            body=data.get("body", ""),
            to_agent_id=data.get("to_agent_id"),
            task_id=data.get("task_id"),
            kind=data.get("kind", "note"),
            thread_id=data.get("thread_id"),
        )

    @api.get("/crews/{team_id}/a2a")
    def a2a_log(team_id: str, status: str | None = None, limit: int = Query(100, ge=1, le=500)) -> dict:
        return {
            "messages": app.a2a.log(team_id, status, limit),
            "stats": app.a2a.stats(team_id),
        }

    @api.get("/crews/{team_id}/a2a/review-queue")
    def review_queue(team_id: str) -> dict:
        return {"pending": app.a2a.review_queue(team_id)}

    @api.post("/a2a/{message_id}/review")
    def review_message(message_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.a2a.review(
            message_id,
            data.get("decision", ""),
            reviewer=str(data.get("reviewer", "owner")),
            note=data.get("note", ""),
        )

    @api.get("/a2a/{message_id}")
    def get_message(message_id: str) -> dict:
        return app.a2a.get(message_id)

    @api.get("/threads/{thread_id}")
    def get_thread(thread_id: str) -> dict:
        return {"messages": app.a2a.thread(thread_id)}

    # ---- workflows ----------------------------------------------------
    @api.post("/crews/{team_id}/workflows")
    def save_workflow(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.workflows.save(team_id, data.get("name", ""), data.get("definition") or {})

    @api.get("/crews/{team_id}/workflows")
    def list_workflows(team_id: str) -> dict:
        return {"workflows": app.workflows.list(team_id)}

    @api.get("/workflows/{workflow_id}")
    def get_workflow(workflow_id: str) -> dict:
        return app.workflows.get(workflow_id)

    @api.post("/workflows/{workflow_id}/run")
    def run_workflow(workflow_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload or {})
        return app.workflows.run(workflow_id, data.get("trigger") or {})

    @api.get("/workflow-runs/{run_id}")
    def get_workflow_run(run_id: str) -> dict:
        return app.workflows.get_run(run_id)

    @api.post("/workflow-runs/{run_id}/resume")
    def resume_workflow(run_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.workflows.resume(
            run_id, data.get("node_id", ""), data.get("decision", "approved"), data.get("note", "")
        )

    # ---- schedules ----------------------------------------------------
    @api.post("/crews/{team_id}/schedules")
    def create_schedule(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.scheduler.create(
            team_id=team_id,
            name=data.get("name", "schedule"),
            kind=data.get("kind", "interval"),
            spec=str(data.get("spec", "")),
            task_template=data.get("task_template") or {},
        )

    @api.get("/crews/{team_id}/schedules")
    def list_schedules(team_id: str) -> dict:
        return {"schedules": app.scheduler.list(team_id)}

    # ---- imports: an agent does not have to be born here ---------------
    @api.get("/imports/agents/formats")
    def agent_formats() -> dict:
        return {"formats": AGENT_SOURCES}

    @api.post("/crews/{team_id}/agents/import")
    def import_agent_route(team_id: str, payload: dict = Body(...)) -> dict:
        """`source` is a path, a url, the content itself, or a json object. the format
        is detected unless it is named, and `dry_run` returns the manifest."""
        data = body(payload)
        ref = data.get("source")
        if ref is None and data.get("content") is not None:
            ref = data["content"]
        if ref is None and data.get("url"):
            ref = data["url"]
        if ref is None:
            raise ValidationError("give a source, a url or content to import")
        manifest = import_agent(ref, source=data.get("format") or "",
                               agent_name=data.get("entry") or "")
        if data.get("dry_run"):
            return {"manifest": manifest.to_dict(), "imported": False}
        return adopt(app, team_id, manifest, name=data.get("name") or "")

    @api.get("/agents/{agent_id}/export")
    def export_agent_route(agent_id: str) -> dict:
        agent = app.crews.get_agent(agent_id)
        profile = app.profile.profile(agent_id) if app.profile is not None else {}
        return export_agent(agent, profile)

    # ---- sagas: a sequence with a way back ---------------------------
    @api.post("/crews/{team_id}/sagas")
    def create_saga(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        saga = app.sagas.create(team_id, data.get("name", "saga"),
                                data.get("steps") or [], context=data.get("context") or {})
        if data.get("run", True):
            app.scheduler.broker.enqueue("saga", {"saga_id": saga["id"]})
        return saga

    @api.get("/crews/{team_id}/sagas")
    def list_sagas(team_id: str, status: str = "") -> dict:
        return {"sagas": app.sagas.list(team_id, status=status)}

    @api.get("/sagas/{saga_id}")
    def get_saga(saga_id: str) -> dict:
        return app.sagas.get(saga_id)

    @api.post("/sagas/{saga_id}/run")
    def run_saga(saga_id: str) -> dict:
        saga = app.sagas.get(saga_id)
        job = app.scheduler.broker.enqueue("saga", {"saga_id": saga["id"]})
        return {"saga": saga["id"], "status": saga["status"], "job_id": job}

    @api.post("/sagas/{saga_id}/retry")
    def retry_saga(saga_id: str) -> dict:
        job = app.scheduler.broker.enqueue("saga", {"saga_id": saga_id})
        app.sagas.retry_failed(saga_id)
        return {"saga": saga_id, "job_id": job}

    @api.get("/ops")
    def list_ops() -> dict:
        return {"ops": app.ops.names(), "checks": app.checks.names()}

    # ---- polls: wait for the world -------------------------------------
    @api.post("/crews/{team_id}/polls")
    def start_poll(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.polls.start(
            team_id=team_id,
            name=data.get("name", ""),
            check=data.get("check", ""),
            check_args=data.get("check_args") or {},
            until=data.get("until") or {},
            interval=float(data.get("interval", 60)),
            max_interval=float(data.get("max_interval", 3600)),
            backoff=float(data.get("backoff", 1.5)),
            max_attempts=int(data.get("max_attempts", 20)),
            deadline=data.get("deadline"),
            on_satisfy=data.get("on_satisfy") or {},
            on_expire=data.get("on_expire") or {},
            jitter=float(data.get("jitter", 0.0)),
            first_delay=float(data.get("first_delay", 0.0)),
        )

    @api.get("/crews/{team_id}/polls")
    def list_polls(team_id: str, status: str = "") -> dict:
        return {"polls": app.polls.list(team_id, status=status)}

    @api.get("/polls/{poll_id}")
    def get_poll(poll_id: str) -> dict:
        poll = app.polls.get(poll_id)
        poll["attempts_log"] = app.polls.attempts(poll_id)
        return poll

    @api.post("/polls/{poll_id}/cancel")
    def cancel_poll(poll_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload or {})
        return app.polls.cancel(poll_id, reason=data.get("reason", "cancelled by a human"))

    # ---- a2a protocol interop -----------------------------------------
    @api.get("/agents/{agent_id}/card")
    def agent_card(agent_id: str, base_url: str = Query("http://localhost:8000")) -> dict:
        agent = app.crews.get_agent(agent_id)
        crew = app.crews.get_team(agent["team_id"])
        return a2a_spec.agent_card(crew, agent, base_url, crew["review_mode"])

    @api.post("/a2a/rpc")
    def a2a_rpc(payload: dict = Body(...)) -> dict:
        """json-rpc 2.0 binding. only the methods crittertown can honour exactly."""
        data = body(payload)
        rpc_id = data.get("id")
        method = data.get("method")
        params = data.get("params") or {}
        if data.get("jsonrpc") != "2.0" or not method:
            return a2a_spec.rpc_error(rpc_id, a2a_spec.RPC_INVALID_REQUEST, "expected a json-rpc 2.0 request")
        try:
            if method == "message/send":
                message = params.get("message") or params
                parsed = a2a_spec.message_from_json(message)
                metadata = (message.get("metadata") or {}).get("crittertown") or {}
                team_id = metadata.get("crewId") or params.get("crewId")
                if not team_id:
                    raise ValidationError("message/send needs metadata.crittertown.crewId")
                sent = app.a2a.send(
                    team_id=team_id,
                    from_agent_id=parsed["from_agent_id"],
                    body=parsed["body"],
                    to_agent_id=parsed["to_agent_id"],
                    task_id=parsed["task_id"],
                    kind=parsed["kind"],
                    thread_id=parsed["thread_id"],
                )
                result = a2a_spec.message_to_json(sent, role=a2a_spec.ROLE_USER)
                result["metadata"]["crittertown"]["accepted"] = True
                result["metadata"]["crittertown"]["reviewPending"] = sent["status"] == "pending"
                return a2a_spec.rpc_result(rpc_id, result)
            if method == "tasks/get":
                task_id = params.get("id") or params.get("taskId")
                if not task_id:
                    raise ValidationError("tasks/get needs params.id")
                task = app.kanban.get_task(task_id)
                messages = app.a2a.thread(task["id"])
                return a2a_spec.rpc_result(rpc_id, a2a_spec.task_to_json(task, messages))
            if method == "agent/getCard":
                agent_id = params.get("agentId")
                if not agent_id:
                    raise ValidationError("agent/getCard needs params.agentId")
                agent = app.crews.get_agent(agent_id)
                crew = app.crews.get_team(agent["team_id"])
                base = params.get("baseUrl") or "http://localhost:8000"
                return a2a_spec.rpc_result(
                    rpc_id, a2a_spec.agent_card(crew, agent, base, crew["review_mode"])
                )
            return a2a_spec.rpc_error(
                rpc_id, a2a_spec.RPC_METHOD_NOT_FOUND, f"method '{method}' is not supported"
            )
        except NotFound as exc:
            return a2a_spec.rpc_error(rpc_id, a2a_spec.RPC_INVALID_PARAMS, exc.message)
        except ValidationError as exc:
            return a2a_spec.rpc_error(rpc_id, a2a_spec.RPC_INVALID_PARAMS, exc.message, exc.details)

    # ---- workflow import ----------------------------------------------
    @api.post("/crews/{team_id}/workflows/import")
    def import_workflow_route(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        source = data.get("source", "")
        imported = import_workflow(source, data.get("payload"))
        saved = app.workflows.save(team_id, imported.name, imported.definition)
        return {
            "workflow": saved,
            "warnings": imported.warnings,
            "unmapped": imported.unmapped,
            "sources": sorted(SOURCES),
        }

    @api.get("/workflows/sources")
    def workflow_sources() -> dict:
        return {"sources": sorted(SOURCES)}

    @api.post("/schedules/{schedule_id}/enable")
    def enable_schedule(schedule_id: str, payload: dict = Body(default={})) -> dict:
        enabled = bool((payload or {}).get("enabled", True))
        return app.scheduler.set_enabled(schedule_id, enabled)

    # ---- the front door: an outcome in, a plan out ---------------------
    @api.post("/crews/{team_id}/ask")
    def ask_crew(team_id: str, payload: dict = Body(...),
                 idempotency_key: str = Query(default="", alias="idempotency_key")) -> dict:
        data = body(payload)
        return app.asks.ask(
            team_id,
            text=data.get("text", ""),
            mode=data.get("mode", "plan"),
            channel_id=data.get("channel_id"),
            due=data.get("due", ""),
            constraints=data.get("constraints") or {},
            actor=data.get("actor", "owner"),
            idempotency_key=idempotency_key or data.get("idempotency_key", ""),
        )

    @api.get("/crews/{team_id}/asks")
    def list_asks(team_id: str, limit: int = Query(default=20)) -> dict:
        return {"asks": app.asks.list(team_id, limit=limit)}

    @api.get("/asks/{ask_id}")
    def get_ask(ask_id: str) -> dict:
        return app.asks.get(ask_id)

    @api.post("/asks/{ask_id}/approve")
    def approve_ask(ask_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload) if payload else {}
        return app.asks.approve(ask_id, actor=data.get("actor", "owner"),
                                dispatch=bool(data.get("dispatch", True)))

    @api.post("/asks/{ask_id}/dispatch")
    def dispatch_ask(ask_id: str) -> dict:
        return app.asks.dispatch(ask_id)

    # ---- the morning brief -------------------------------------------
    @api.post("/crews/{team_id}/brief")
    def build_brief(team_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload) if payload else {}
        return app.brief.build(team_id, date=data.get("date"),
                               include_a2a=bool(data.get("include_a2a")),
                               store=bool(data.get("store", True)))

    @api.get("/crews/{team_id}/brief")
    def get_brief(team_id: str, date: str = Query(default="")) -> dict:
        if date:
            return app.brief.get(team_id, date)
        return app.brief.build(team_id, store=False)

    @api.get("/crews/{team_id}/briefs")
    def list_briefs(team_id: str, limit: int = Query(default=14)) -> dict:
        return {"briefs": app.brief.list(team_id, limit=limit)}

    @api.get("/agents/{agent_id}/brief")
    def agent_brief(agent_id: str, date: str = Query(default="")) -> dict:
        agent = app.crews.get_agent(agent_id)
        return app.brief.build(agent["team_id"], date=date or None, agent_id=agent_id,
                               store=False)

    # ---- approvals without a login -----------------------------------
    @api.get("/g/{token}", response_class=HTMLResponse)
    def gate_public(token: str) -> str:
        try:
            preview = app.gates.preview(token)
        except CritterTownError as exc:
            return gate_page("this approval link does not work",
                             f"<p>{exc.message}</p><p class='note'>ask whoever sent it "
                             "for a fresh link; nothing was changed.</p>")
        gate = preview["gate"]
        if preview["already"]:
            decided = gate["decided"] or "closed"
            return gate_page(
                gate["title"],
                f"<p class='done'>already {decided}. nothing else to do.</p>",
                decision=decided,
                kicker="already answered",
            )
        if not preview["usable"]:
            return gate_page(gate["title"],
                             "<p>this link has expired. the run is still parked and the "
                             "card is still in review.</p>", kicker="expired")
        decision = preview["decision"]
        verb = "approved" if decision == "approved" else (
            "rejected" if decision == "rejected" else decision)
        prompt = f"<p>{gate['prompt']}</p>" if gate.get("prompt") else ""
        return gate_page(
            gate["title"],
            f"{prompt}<form method='post' action='/g/{token}'>"
            f"<button type='submit'>{verb}</button></form>"
            "<p class='note'>one tap. no account, no login, and the link stops working "
            "after this.</p>",
            decision=decision,
            kicker=f"needs {verb}",
        )

    @api.post("/g/{token}", response_class=HTMLResponse)
    def gate_public_decide(token: str) -> str:
        try:
            outcome = app.gates.decide(token, actor="owner")
        except CritterTownError as exc:
            return gate_page("that did not go through", f"<p>{exc.message}</p>")
        gate = outcome["gate"]
        decided = outcome["decision"]
        extra = ""
        if outcome.get("already"):
            extra = "<p class='note'>it was already answered earlier.</p>"
        if outcome.get("run"):
            extra += f"<p class='note'>run is now <code>{outcome['run'].get('status')}</code>.</p>"
        return gate_page(gate["title"],
                         f"<p class='done'>{decided}. recorded, and the record says who and "
                         f"when.</p>{extra}", decision=decided, kicker="done")

    @api.get("/gates")
    def list_gates(team_id: str = Query(...), status: str = Query(default="")) -> dict:
        return {"gates": app.gates.list(team_id, status=status or None)}

    @api.get("/gates/{gate_id}")
    def get_gate(gate_id: str) -> dict:
        return app.gates.get(gate_id)

    @api.post("/gates/{gate_id}/decide")
    def decide_gate(gate_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.gates.decide(gate_id, decision=data.get("decision", ""),
                                actor=data.get("actor", "owner"),
                                note=data.get("note", ""))

    # ---- workspace connectors and per-agent access -------------------
    @api.post("/crews/{team_id}/connectors")
    def connect_workspace(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.connectors.connect(
            team_id,
            kind=data.get("kind", ""),
            name=data.get("name", ""),
            config=data.get("config") or {},
            secret_env=data.get("secret_env", ""),
            actor=data.get("actor", "owner"),
        )

    @api.get("/crews/{team_id}/connectors")
    def list_connectors(team_id: str) -> dict:
        return {"connectors": app.connectors.list(team_id),
                "kinds": sorted(ADAPTERS)}

    @api.delete("/connectors/{connector_id}")
    def disconnect(connector_id: str) -> dict:
        return app.connectors.disconnect(connector_id)

    @api.get("/connectors/{connector_id}/probe")
    def probe_connector(connector_id: str) -> dict:
        return app.connectors.probe(connector_id)

    @api.post("/connectors/{connector_id}/sync")
    def sync_connector(connector_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload) if payload else {}
        return app.connectors.sync(connector_id, limit=int(data.get("limit", 50)))

    @api.get("/connectors/{connector_id}/docs")
    def connector_docs(connector_id: str, limit: int = Query(default=50)) -> dict:
        return {"docs": app.connectors.docs(connector_id, limit=limit)}

    @api.post("/agents/{agent_id}/access")
    def grant_access(agent_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.connectors.grant(
            agent_id,
            data.get("connector_id", ""),
            supervisor_id=data.get("supervisor_id", ""),
            expires_hours=float(data.get("expires_hours", 24)),
            scopes=data.get("scopes"),
            actor=data.get("actor", "owner"),
        )

    @api.delete("/agents/{agent_id}/access/{connector_id}")
    def revoke_access(agent_id: str, connector_id: str,
                      supervisor_id: str = Query(default=""),
                      reason: str = Query(default="")) -> dict:
        return app.connectors.revoke(agent_id, connector_id, supervisor_id=supervisor_id,
                                     reason=reason)

    @api.get("/agents/{agent_id}/access")
    def agent_access(agent_id: str) -> dict:
        agent = app.crews.get_agent(agent_id)
        return {"agent_id": agent_id,
                "grants": [g for g in app.profile.grants(agent_id)
                           if g.get("kind") == "connectors"],
                "connectors": app.connectors.list(agent["team_id"])}

    @api.get("/agents/{agent_id}/supervised-access")
    def supervised_access(agent_id: str) -> dict:
        return app.connectors.access(supervisor_id=agent_id)

    @api.get("/agents/{agent_id}/knowledge")
    def agent_knowledge(agent_id: str, q: str = Query(default=""),
                        limit: int = Query(default=10),
                        connector_id: str = Query(default="")) -> dict:
        return app.connectors.search(agent_id, q, limit=limit,
                                     connector_id=connector_id or None)

    # ---- packs: reviewed rules a crew can adopt in one call -----------
    # ---- intent: what the crew is for, above the cards -----------------
    @api.get("/crews/{team_id}/intent")
    def crew_intent(team_id: str, agent_id: str = Query(default="")) -> dict:
        """the live intent, plus any proposal waiting on a person. a proposal steers
        nothing until it is approved, which is the whole point of the two states."""
        summary = app.intent.summary(team_id, agent_id)
        summary["history"] = app.intent.history(team_id, agent_id or None)[:20]
        return summary

    @api.post("/crews/{team_id}/intent")
    def record_intent(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.intent.record(
            team_id,
            data.get("statement", ""),
            kind=data.get("kind", "direction"),
            agent_id=data.get("agent_id", ""),
            rationale=data.get("rationale", ""),
            status=data.get("status", "active"),
            actor=data.get("actor", "human"),
        )

    @api.post("/intent/{intent_id}/approve")
    def approve_intent(intent_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload)
        return app.intent.approve(intent_id, actor=data.get("actor", "human"),
                                  edited=data.get("statement", ""))

    @api.post("/intent/{intent_id}/reject")
    def reject_intent(intent_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload)
        return app.intent.reject(intent_id, reason=data.get("reason", ""),
                                 actor=data.get("actor", "human"))

    # ---- routines: recurring work with a receipt -----------------------
    @api.get("/crews/{team_id}/routines")
    def list_routines(team_id: str, on: str = Query(default="")) -> dict:
        return {"team_id": team_id, "routines": app.routines.list(team_id, on=on or None),
                "summary": app.routines.summary(team_id)}

    @api.post("/crews/{team_id}/routines")
    def create_routine(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.routines.create(
            team_id,
            data.get("name", ""),
            description=data.get("description", ""),
            cadence=data.get("cadence", "daily"),
            agent_id=data.get("agent_id", ""),
            weekday=data.get("weekday", 0),
            hour=int(data.get("hour", 8)),
            steps=list(data.get("steps") or []),
            required_skills=list(data.get("required_skills") or []),
        )

    @api.post("/crews/{team_id}/routines/materialise")
    def materialise_routines(team_id: str, on: str = Query(default="")) -> dict:
        """idempotent per (routine, date): calling it twice is how an operator checks
        without being afraid of the button."""
        return app.routines.materialise(team_id, on=on or None)

    @api.post("/routines/{routine_id}/done")
    def complete_routine(routine_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload)
        return app.routines.complete(routine_id, on=data.get("on") or None,
                                     notes=data.get("notes", ""))

    @api.post("/routines/{routine_id}/pause")
    def pause_routine(routine_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload)
        return app.routines.pause(routine_id, status=data.get("status", "paused"))

    # ---- guardrails: the limits that answer without a person ------------
    @api.get("/crews/{team_id}/guardrails")
    def list_guardrails(team_id: str, agent_id: str | None = Query(default=None)) -> dict:
        return {"guardrails": app.guardrails.list(team_id, agent_id=agent_id),
                "refused_today": app.guardrails.summary(team_id)}

    @api.post("/crews/{team_id}/guardrails")
    def set_guardrail(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.guardrails.set(
            team_id,
            str(data.get("kind") or ""),
            data.get("value"),
            agent_id=str(data.get("agent_id") or ""),
            actor=str(data.get("actor") or "human"),
        )

    @api.delete("/guardrails/{rail_id}")
    def retire_guardrail(rail_id: str, actor: str = Query(default="human")) -> dict:
        return app.guardrails.retire(rail_id, actor=actor)

    @api.post("/crews/{team_id}/guardrails/check")
    def check_guardrails(team_id: str, payload: dict = Body(default_factory=dict)) -> dict:
        data = body(payload)
        return app.guardrails.check(
            team_id, str(data.get("action") or "dispatch"), data.get("context") or {})

    # ---- self improvement: propose, dispose, promote -------------------
    @api.get("/agents/{agent_id}/lessons")
    def list_lessons(agent_id: str, status: str = Query(default="")) -> dict:
        return {"lessons": app.lessons.list(agent_id, status=status or None)}

    @api.post("/agents/{agent_id}/lessons")
    def propose_lesson(agent_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.lessons.propose(
            agent_id,
            str(data.get("statement") or ""),
            run_id=str(data.get("run_id") or ""),
            actor=str(data.get("actor") or "agent"),
        )

    @api.get("/crews/{team_id}/lessons/candidates")
    def lesson_candidates(team_id: str, limit: int = Query(default=10)) -> dict:
        return {"runs": app.lessons.candidates(team_id, limit=limit)}

    @api.post("/lessons/{lesson_id}/approve")
    def approve_lesson(lesson_id: str, payload: dict = Body(default_factory=dict)) -> dict:
        data = body(payload)
        return app.lessons.approve(lesson_id, statement=data.get("statement"),
                                   actor=str(data.get("actor") or "human"))

    @api.post("/lessons/{lesson_id}/reject")
    def reject_lesson(lesson_id: str, payload: dict = Body(default_factory=dict)) -> dict:
        data = body(payload)
        return app.lessons.reject(lesson_id, reason=str(data.get("reason") or ""),
                                  actor=str(data.get("actor") or "human"))

    @api.post("/lessons/{lesson_id}/promote")
    def promote_lesson(lesson_id: str, payload: dict = Body(default_factory=dict)) -> dict:
        data = body(payload)
        return app.lessons.promote(lesson_id, key=str(data.get("key") or ""),
                                   actor=str(data.get("actor") or "human"))

    # ---- the curated knowledge base ------------------------------------
    @api.get("/crews/{team_id}/knowledge")
    def search_knowledge(team_id: str, q: str = Query(default=""),
                         kind: str = Query(default=""), agent_id: str = Query(default=""),
                         limit: int = Query(default=20)) -> dict:
        return {
            "entries": app.knowledge.search(team_id, q, kind=kind, agent_id=agent_id,
                                            limit=limit),
            "stats": app.knowledge.stats(team_id),
            "kinds": list(KNOWLEDGE_KINDS),
        }

    @api.post("/crews/{team_id}/knowledge")
    def record_knowledge(team_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.knowledge.record(
            team_id,
            kind=str(data.get("kind") or ""),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            agent_id=str(data.get("agent_id") or ""),
            symptom=str(data.get("symptom") or ""),
            workaround=str(data.get("workaround") or ""),
            tags=list(data.get("tags") or []),
            source=str(data.get("source") or ""),
            actor=str(data.get("actor") or "human"),
        )

    @api.post("/knowledge/{entry_id}")
    def update_knowledge(entry_id: str, payload: dict = Body(default_factory=dict)) -> dict:
        data = body(payload)
        return app.knowledge.update(
            entry_id,
            body=data.get("body"),
            tags=data.get("tags"),
            status=data.get("status"),
            actor=str(data.get("actor") or "human"),
        )

    @api.delete("/knowledge/{entry_id}")
    def retire_knowledge(entry_id: str, actor: str = Query(default="human")) -> dict:
        return app.knowledge.retire(entry_id, actor=actor)

    @api.post("/crews/{team_id}/triggers/pack")
    def install_pack(team_id: str, payload: dict = Body(default={})) -> dict:
        data = body(payload) if payload else {}
        return app.triggers.install_pack(
            team_id,
            pack=data.get("pack", "overnight"),
            dry_run=bool(data.get("dry_run", True)),
        )

    # ---- brokered credentials, per agent -------------------------------
    @api.get("/vault")
    def vault_health() -> dict:
        return app.vault.health()

    @api.post("/agents/{agent_id}/vault")
    def grant_vault(agent_id: str, payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.vault.grant(
            agent_id,
            label=data.get("label", ""),
            token_env=data.get("token_env", ""),
            proxy_url=data.get("proxy_url", ""),
            scopes=data.get("scopes") or [],
            expires_hours=data.get("expires_hours"),
            issued_by=data.get("actor", "owner"),
        )

    @api.get("/agents/{agent_id}/vault")
    def list_vault(agent_id: str) -> dict:
        return {"grants": app.vault.list(agent_id=agent_id),
                "env": app.vault.describe_env(agent_id)}

    @api.get("/agents/{agent_id}/sandbox-env")
    def sandbox_env(agent_id: str) -> dict:
        """the env a sandbox pulls at boot, instead of receiving it at create time.

        the values are resolved from this process's environment at call time: never
        stored, never logged, and returned only to a caller that already holds the
        platform token, which is the agent's own harness. a missing token is a 409 with
        the grant named, because a sandbox that boots without its proxy is worse than one
        that fails to boot.
        """
        app.crews.get_agent(agent_id)
        return {"agent_id": agent_id, "env": app.vault.sandbox_env(agent_id)}

    @api.delete("/vault/{grant_id}")
    def revoke_vault(grant_id: str, reason: str = Query(default=""),
                     actor: str = Query(default="owner")) -> dict:
        return app.vault.revoke(grant_id, actor=actor, reason=reason)

    @api.post("/vault/egress")
    def vault_egress(payload: dict = Body(...)) -> dict:
        data = body(payload)
        return app.vault.egress_allowed(data.get("agent_id", ""), data.get("host", ""))

    return api
