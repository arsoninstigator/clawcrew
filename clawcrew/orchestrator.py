"""the orchestrator: turns a kanban task into a sandboxed agent run.

the loop per task:

1. validate the task and the crew, move it to `in_progress`
2. choose an agent (explicit assignee wins; otherwise capability match)
3. build the agent's computer - the agent's persistent one, or a throwaway
   ephemeral sandbox when the task asks for isolation
4. run up to `max_turns` model turns, executing shell commands in the sandbox and
   logging every one
5. post the agent's outgoing message through the a2a gate
6. write the result, then move the card to `review` (reviewed crews) or `done`
7. destroy ephemeral sandboxes, never the persistent ones

every step is persisted, so a crashed worker leaves a readable `runs` row instead
of an orphaned process. the orchestrator holds no state between calls.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .a2a import A2AService
from .config import Settings
from .crews import CrewService
from .db import Store, dumps, loads, new_id, now
from .errors import ClawCrewError, NotFound, ValidationError
from .kanban import KanbanService
from .runtime import AgentModel, build_model, render_prompt
from .sandbox import Handle, ProviderRegistry


class CrewRunner:
    # injected by the composition root; a run that fails is an event, not a log line
    events_hook = None
    def __init__(
        self,
        store: Store,
        registry: ProviderRegistry,
        settings: Settings,
        model: AgentModel | None = None,
    ):
        self.store = store
        self.registry = registry
        self.settings = settings
        self.model = model or build_model(settings)
        self.crews = CrewService(store, registry)
        self.kanban = KanbanService(store)
        self.a2a = A2AService(store)

    # ---- agent selection ----------------------------------------------
    def pick_agent(self, team_id: str, task: dict) -> dict:
        agents = [a for a in self.crews.list_agents(team_id) if a["status"] != "retired"]
        if task.get("assignee_agent_id"):
            for agent in agents:
                if agent["id"] == task["assignee_agent_id"]:
                    return agent
            raise NotFound(f"assigned agent {task['assignee_agent_id']} is not in this crew")
        if not agents:
            raise ValidationError("this crew has no agents to run the task")
        required = {s.lower() for s in task.get("required_skills") or []}
        words = {w.lower() for w in (task.get("title", "") + " " + task.get("description", "")).split() if len(w) > 3}

        def score(agent: dict) -> tuple[int, int]:
            skills = {s.lower() for s in agent["skills"]}
            role = {w.lower() for w in (agent["role"] or "").split() if len(w) > 3}
            skill_hits = len(required & skills) * 4 + len(words & (skills | role))
            return (skill_hits, 1 if agent["status"] == "idle" else 0)

        return sorted(agents, key=score, reverse=True)[0]

    # ---- planning ------------------------------------------------------
    def plan(self, task_id: str) -> list[dict]:
        """expand a task into subtasks.

        explicit subtasks in the task payload win. otherwise the crew policy
        decides whether the task stays whole. no model call here on purpose:
        planning must be cheap and reproducible, the model does the work.
        """
        task = self.kanban.get_task(task_id)
        team = self.crews.get_team(task["team_id"])
        policy = team["orchestration"]
        declared = task["payload"].get("subtasks") if isinstance(task["payload"], dict) else None
        if not isinstance(declared, list) or not declared:
            if not policy.get("decompose") or task["payload"].get("subtasks") is False:
                return []
            return []
        created = []
        for index, item in enumerate(declared):
            if not isinstance(item, dict) or not item.get("title"):
                continue
            created.append(
                self.kanban.create_task(
                    team_id=task["team_id"],
                    title=str(item["title"])[:200],
                    description=str(item.get("description", "")),
                    column="ready",
                    priority=int(item.get("priority", task["priority"])),
                    required_skills=list(item.get("required_skills") or []),
                    isolation=item.get("isolation", task["isolation"]),
                    payload=item.get("payload") or {},
                    parent_id=task["id"],
                )
            )
        return created

    # ---- the run -------------------------------------------------------
    def dispatch(self, task_id: str, max_turns: int = 6, actor: str = "orchestrator") -> dict:
        task = self.kanban.get_task(task_id)
        team = self.crews.get_team(task["team_id"])
        if task["column"] not in {"ready", "in_progress"}:
            raise ValidationError(
                f"task is in '{task['column']}'; move it to 'ready' before dispatching"
            )
        agent = self.pick_agent(task["team_id"], task)
        if task["column"] == "ready":
            task = self.kanban.move(task_id, "in_progress", actor)
        self.crews.update_agent(agent["id"], status="working")

        run_id = new_id("run")
        provider = None
        handle: Handle | None = None
        steps: list[dict] = []
        error = ""
        result: dict = {}
        try:
            provider, handle = self._sandbox(agent, task)
            self._insert_run(run_id, task, agent, provider.name, handle.ref)
            inbox = self.a2a.inbox(agent["id"], limit=10)
            context = self.crews.context(task["team_id"])["values"]
            prompt = render_prompt(task, agent, team, context, inbox)
            system_prompt = agent["system_prompt"] or (
                f"you are {agent['name']}, {agent['role'] or 'a member'} of the {team['name']} crew."
            )
            done = False
            for turn in range(1, max_turns + 1):
                action = self.model.act(system_prompt, prompt)
                turn_steps = []
                for command in action.get("commands", []):
                    outcome = provider.exec(handle, command, timeout=min(self.settings.sandbox_timeout_seconds, 600))
                    turn_steps.append(
                        {"kind": "command", "command": command, **outcome.to_dict()}
                    )
                    prompt += f"\n\n$ {command}\n{outcome.stdout[-1500:]}{outcome.stderr[-500:]}"
                message = action.get("message")
                if message:
                    posted = self._post_message(team, agent, task, message)
                    turn_steps.append({"kind": "a2a", "message_id": posted["id"], "status": posted["status"]})
                steps.append({"turn": turn, "thought": action.get("thought", ""), "steps": turn_steps})
                self._update_run(run_id, steps)
                if action.get("done"):
                    done = True
                    result = action.get("result") or {}
                    break
            if not done:
                result = {"summary": "turn budget exhausted", "turns": max_turns}
                steps.append({"turn": max_turns, "thought": "turn budget exhausted", "steps": []})
        except ClawCrewError as exc:
            error = f"{exc.code}: {exc.message}"
            self._update_run(run_id, steps, status="failed", error=error)
            self.kanban.move(task_id, "blocked", agent["id"], note=error[:500])
            self.crews.update_agent(agent["id"], status="idle")
            if self.events_hook is not None:
                self.events_hook("run.failed", {
                    "team_id": task["team_id"], "task_id": task_id, "run_id": run_id,
                    "agent_id": agent["id"], "error": error, "actor": agent["id"],
                })
            raise
        finally:
            if provider is not None and handle is not None and handle.kind == "ephemeral":
                try:
                    provider.destroy(handle)
                except ClawCrewError:
                    pass

        self._update_run(run_id, steps, status="succeeded")
        self.crews.update_agent(agent["id"], status="idle")
        self.crews.put_context(
            task["team_id"],
            f"task:{task_id}:result",
            {"agent": agent["name"], "result": result},
            author=agent["name"],
        )
        reviewed = team["review_mode"] == "reviewed" and "review" in self.kanban.columns(team["id"])
        task = self.kanban.complete(
            task_id, {**result, "run_id": run_id, "agent_id": agent["id"]},
            actor=agent["id"], column="review" if reviewed else "done",
        )
        return {
            "run_id": run_id,
            "task": task,
            "agent": {"id": agent["id"], "name": agent["name"]},
            "provider": provider.name if provider else "",
            "sandbox": handle.ref if handle else "",
            "steps": steps,
            "result": result,
            "error": error,
        }

    def dispatch_ready(self, team_id: str, limit: int = 10, max_turns: int = 6) -> list[dict]:
        ready = self.kanban.list_tasks(team_id, column="ready", limit=limit)
        if not ready:
            return []
        workers = max(1, min(self.settings.max_parallel_runs, len(ready)))
        outcomes: list[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.dispatch, t["id"], max_turns): t for t in ready}
            for future, task in futures.items():
                try:
                    outcomes.append(future.result())
                except ClawCrewError as exc:
                    outcomes.append({"task": task, "error": exc.to_dict()})
        return outcomes

    # ---- sandbox -------------------------------------------------------
    def _sandbox(self, agent: dict, task: dict) -> tuple[Any, Handle]:
        runtime = agent["runtime"] or {}
        if task["isolation"] == "ephemeral":
            provider = self.registry.get("ephemeral")
            spec = {
                "kind": "ephemeral",
                "template": runtime.get("template") or self.settings.e2b_default_template,
                "ttl_seconds": runtime.get("ttl_seconds") or self.settings.sandbox_timeout_seconds,
            }
            return provider, provider.ensure(f"task-{task['id']}", spec)
        computer = self.crews.ensure_computer(agent["id"])
        provider = self.registry.get(computer["provider"])
        return provider, Handle.from_dict(computer["handle"])

    # ---- runs ----------------------------------------------------------
    def _insert_run(self, run_id: str, task: dict, agent: dict, provider: str, sandbox: str) -> None:
        with self.store.tx() as c:
            c.execute(
                """insert into runs (id, task_id, agent_id, provider, sandbox_id, status, steps,
                       started_at, created_date) values (?,?,?,?,?, 'running', '[]', ?, ?)""",
                (run_id, task["id"], agent["id"], provider, sandbox, now(), now()),
            )

    def _update_run(self, run_id: str, steps: list[dict], status: str | None = None, error: str = "") -> None:
        with self.store.tx() as c:
            c.execute("update runs set steps = ? where id = ?", (dumps(steps), run_id))
            if status:
                c.execute(
                    "update runs set status = ?, error = ?, ended_at = ? where id = ?",
                    (status, error, now(), run_id),
                )

    def get_run(self, run_id: str) -> dict:
        row = self.store.one("select * from runs where id = ?", (run_id,))
        if not row:
            raise NotFound(f"run {run_id} not found")
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "agent_id": row["agent_id"],
            "provider": row["provider"],
            "sandbox_id": row["sandbox_id"],
            "status": row["status"],
            "steps": loads(row["steps"], []),
            "error": row["error"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
        }

    def list_runs(self, task_id: str) -> list[dict]:
        return [
            {"id": r["id"], "status": r["status"], "agent_id": r["agent_id"],
             "provider": r["provider"], "created_date": r["created_date"]}
            for r in self.store.query(
                "select * from runs where task_id = ? order by created_date desc", (task_id,)
            )
        ]

    # ---- messages ------------------------------------------------------
    def _post_message(self, team: dict, agent: dict, task: dict, message: dict) -> dict:
        to_name = message.get("to")
        to_agent_id = None
        if to_name:
            match = [a for a in self.crews.list_agents(team["id"]) if a["name"] == to_name]
            if not match:
                raise ValidationError(f"message recipient '{to_name}' is not in this crew")
            to_agent_id = match[0]["id"]
        kind = message.get("kind") or "note"
        return self.a2a.send(
            team_id=team["id"],
            from_agent_id=agent["id"],
            body=str(message.get("body", ""))[:8000],
            to_agent_id=to_agent_id,
            task_id=task["id"],
            kind=kind if kind in {"note", "handoff", "request", "result", "blocker", "review_request"} else "note",
        )
