"""http surface.

one rule: the api holds no logic, it maps http onto the services and maps the
`ClawCrewError` ladder onto status codes. anything that needs behaviour belongs in
a service so the worker, the cli and tests all get it.

groups:
  /crews        teams, agents, persistent computers, shared context
  /tasks        kanban board, events, dispatch
  /a2a          message send, review queue, review decisions, inbox, log
  /workflows    dag save/validate/run/resume
  /schedules    interval and cron schedules
  /channels     rooms, each with its own board and conversation
  /departments  the org chart, coordinator routing, escalation
  /pov          the workspace as one agent sees it (board, calendar, inbox)
  /triggers     event -> queued action rules
  /ops          health, providers, queue tick, worker drain, counts
"""

from __future__ import annotations

from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import a2a_protocol as a2a_spec
from .app import App, build_app
from .errors import ClawCrewError, NotFound, ValidationError
from .importers import SOURCES, import_workflow


def create_api(app: App | None = None) -> FastAPI:
    app = app or build_app()
    api = FastAPI(
        title="clawcrew",
        version="0.1.0",
        description="agent crews with persistent computers, reviewable a2a, and visual workflows",
    )

    @api.middleware("http")
    async def require_token(request: Request, call_next):
        """single shared token. the backend is meant to sit behind your own auth
        proxy; this is the floor, not a user model."""
        token = app.settings.api_token
        if not token or request.url.path == "/health" or request.method == "OPTIONS":
            return await call_next(request)
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        supplied = supplied or request.headers.get("x-clawcrew-token", "")
        if supplied != token:
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        return await call_next(request)

    @api.exception_handler(ClawCrewError)
    async def _handle(request: Request, exc: ClawCrewError) -> JSONResponse:
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
        """the self-hosted tools clawcrew plugs into: your n8n, your dify."""
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
        )

    @api.get("/crews/{team_id}/agents")
    def list_agents(team_id: str) -> dict:
        return {"agents": app.crews.list_agents(team_id)}

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

    # ---- a2a protocol interop -----------------------------------------
    @api.get("/agents/{agent_id}/card")
    def agent_card(agent_id: str, base_url: str = Query("http://localhost:8000")) -> dict:
        agent = app.crews.get_agent(agent_id)
        crew = app.crews.get_team(agent["team_id"])
        return a2a_spec.agent_card(crew, agent, base_url, crew["review_mode"])

    @api.post("/a2a/rpc")
    def a2a_rpc(payload: dict = Body(...)) -> dict:
        """json-rpc 2.0 binding. only the methods clawcrew can honour exactly."""
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
                metadata = (message.get("metadata") or {}).get("clawcrew") or {}
                team_id = metadata.get("crewId") or params.get("crewId")
                if not team_id:
                    raise ValidationError("message/send needs metadata.clawcrew.crewId")
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
                result["metadata"]["clawcrew"]["accepted"] = True
                result["metadata"]["clawcrew"]["reviewPending"] = sent["status"] == "pending"
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

    return api
