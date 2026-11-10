"""visual workflows: a json dag that the in-app builder edits and the backend runs.

the definition is the product surface. a node graph in json can be rendered by an
n8n/langflow-style canvas, diffed in git, imported from another tool, and executed
by this engine without the canvas needing to know how execution works.

node types:
  agent_task   - create a kanban card and dispatch it to an agent
  shell        - run a command on a specific agent's computer
  set_context  - write crew shared context (the "${...}" templating hook)
  human_gate   - create a card in `review` and pause the workflow until decided
  branch       - pick downstream nodes from state, skipping the rest
  n8n          - POST to a webhook on your self-hosted n8n instance
  dify         - run a workflow on your self-hosted dify instance

the last two are the "plugging" path: clawscrew does not reimplement either tool, it
calls the instance you already run. no n8n or dify source is copied into this repo,
which is what their licences require and what you actually want: your automations
keep living where you built them.

validation is strict on purpose: a dangling dependency or a cycle is rejected at
save time, not at 3am mid-run.
"""

from __future__ import annotations

import re
from typing import Any

from .a2a import A2AService
from .crews import CrewService
from .db import Store, dumps, loads, new_id, now
from .errors import NotFound, ValidationError
from .kanban import KanbanService
from .orchestrator import CrewRunner
from .sandbox import Handle
from .services import build_services

NODE_TYPES = {"agent_task", "shell", "set_context", "human_gate", "branch", "n8n", "dify"}

# draft: a human sketch. proposed: handed in by agents, waiting on approval.
# active: runs. archived: kept, never runs.
STATUSES = ("draft", "proposed", "active", "archived")
TEMPLATE = re.compile(r"\$\{([a-zA-Z0-9_.\-]+)\}")


def validate_definition(definition: Any) -> dict:
    if not isinstance(definition, dict):
        raise ValidationError("workflow definition must be an object")
    nodes = definition.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValidationError("workflow needs a non-empty nodes array")
    ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValidationError("each node must be an object")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise ValidationError("every node needs a string id")
        if node_id in ids:
            raise ValidationError(f"duplicate node id '{node_id}'")
        ids.add(node_id)
        if node.get("type") not in NODE_TYPES:
            raise ValidationError(
                f"node '{node_id}' has unknown type '{node.get('type')}'", allowed=sorted(NODE_TYPES)
            )
    for node in nodes:
        for dep in node.get("depends_on") or []:
            if dep not in ids:
                raise ValidationError(f"node '{node['id']}' depends on unknown node '{dep}'")
        if node["type"] == "n8n" and not (node.get("path") or node.get("webhook")):
            raise ValidationError(f"node '{node['id']}' is an n8n node and needs a webhook 'path'")
        if node["type"] == "branch":
            targets = node.get("targets") or []
            if not isinstance(targets, list) or not targets:
                raise ValidationError(f"branch '{node['id']}' needs a targets array")
            for target in targets:
                if target not in ids:
                    raise ValidationError(f"branch '{node['id']}' targets unknown node '{target}'")
        if node["type"] == "agent_task" and not node.get("instruction") and not node.get("title"):
            raise ValidationError(f"agent_task '{node['id']}' needs a title or instruction")
        if node["type"] == "shell" and not node.get("command"):
            raise ValidationError(f"shell node '{node['id']}' needs a command")
    # cycle detection (kahn)
    indegree = {n["id"]: len([d for d in (n.get("depends_on") or []) if d in ids]) for n in nodes}
    edges: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for node in nodes:
        for dep in node.get("depends_on") or []:
            if dep in ids:
                edges[dep].append(node["id"])
    queue = [nid for nid, deg in indegree.items() if deg == 0]
    seen = 0
    while queue:
        current = queue.pop()
        seen += 1
        for nxt in edges[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if seen != len(ids):
        raise ValidationError("workflow graph has a cycle")
    return definition


class WorkflowService:
    gates = None      # GateService, injected by the composition root
    channels = None   # ChannelService, for gate delivery
    def __init__(self, store: Store, runner: CrewRunner, services=None):
        self.store = store
        self.runner = runner
        self.services = services if services is not None else build_services()
        self.events_hook = None
        self.audit = None
        self.scheduler = None
        self.crews = CrewService(store, getattr(runner, "registry", None))
        self.kanban = KanbanService(store)
        self.a2a = A2AService(store)

    # ---- crud ----------------------------------------------------------
    def save(
        self,
        team_id: str,
        name: str,
        definition: dict,
        status: str | None = None,
        proposed_by_agent_id: str | None = None,
    ) -> dict:
        self.crews.get_team(team_id)
        validate_definition(definition)
        if status is not None and status not in STATUSES:
            raise ValidationError(f"status must be one of {sorted(STATUSES)}")
        existing = self.store.one("select * from workflows where team_id = ? and name = ?", (team_id, name))
        ts = now()
        if existing:
            # editing a workflow never quietly promotes it: only approve() does that.
            with self.store.tx() as c:
                c.execute(
                    """update workflows set definition = ?, version = version + 1, updated_date = ?
                       where id = ?""",
                    (dumps(definition), ts, existing["id"]),
                )
            return self.get(existing["id"])
        workflow_id = new_id("wf")
        with self.store.tx() as c:
            c.execute(
                """insert into workflows
                   (id, team_id, name, version, definition, status, proposed_by_agent_id,
                    created_date, updated_date)
                   values (?,?,?,1,?,?,?,?,?)""",
                (workflow_id, team_id, name, dumps(definition), status or "active",
                 proposed_by_agent_id, ts, ts),
            )
        return self.get(workflow_id)

    def get(self, workflow_id: str) -> dict:
        row = self.store.one("select * from workflows where id = ?", (workflow_id,))
        if not row:
            raise NotFound(f"workflow {workflow_id} not found")
        return {
            "id": row["id"], "team_id": row["team_id"], "name": row["name"],
            "version": row["version"], "definition": loads(row["definition"], {}),
            "status": row["status"] if "status" in row.keys() else "active",
            "proposed_by_agent_id": row["proposed_by_agent_id"] if "proposed_by_agent_id" in row.keys() else None,
            "approved_by": row["approved_by"] if "approved_by" in row.keys() else "",
            "approved_date": row["approved_date"] if "approved_date" in row.keys() else None,
            "coauthors": self.coauthors(row["id"]),
            "created_date": row["created_date"], "updated_date": row["updated_date"],
        }

    def list(self, team_id: str, status: str | None = None) -> list[dict]:
        if status:
            if status not in STATUSES:
                raise ValidationError(f"status must be one of {sorted(STATUSES)}")
            rows = self.store.query(
                "select * from workflows where team_id = ? and status = ? order by created_date",
                (team_id, status),
            )
        else:
            rows = self.store.query(
                "select * from workflows where team_id = ? order by created_date", (team_id,)
            )
        return [
            {
                "id": r["id"], "name": r["name"], "version": r["version"],
                "status": r["status"] if "status" in r.keys() else "active",
                "updated_date": r["updated_date"],
            }
            for r in rows
        ]

    # ---- authoring: an agent can write one, a human still turns it on ----
    def propose(
        self,
        team_id: str,
        name: str,
        definition: dict,
        agent_ids: list[str],
        note: str = "",
    ) -> dict:
        """one or more agents hand in a workflow. it is inert until approved."""
        if not agent_ids:
            raise ValidationError("a proposal needs at least one authoring agent")
        for agent_id in agent_ids:
            agent = self.crews.get_agent(agent_id)
            if agent["team_id"] != team_id:
                raise ValidationError("authoring agents must be in the crew")
        workflow = self.save(
            team_id, name, definition, status="proposed", proposed_by_agent_id=agent_ids[0]
        )
        if workflow["status"] != "proposed":
            raise ValidationError(
                f"workflow '{name}' is already {workflow['status']}; propose under a new name"
            )
        with self.store.tx() as c:
            c.execute("update workflows set status = 'proposed', proposed_by_agent_id = ? where id = ?",
                      (agent_ids[0], workflow["id"]))
            for agent_id in agent_ids:
                c.execute(
                    """insert into workflow_coauthors (workflow_id, agent_id, role, verdict, note, created_date)
                       values (?,?,'author','pending',?,?)
                       on conflict(workflow_id, agent_id) do update set role = 'author'""",
                    (workflow["id"], agent_id, note, now()),
                )
        proposed = self.get(workflow["id"])
        if self.audit is not None:
            for agent_id in agent_ids:
                self.audit.record(
                    team_id=team_id, agent_id=agent_id, action="workflow.proposed",
                    target_kind="workflow", target_id=workflow["id"],
                    payload={"name": name, "note": note, "authors": agent_ids},
                )
        self._emit("workflow.proposed", {"team_id": team_id, "workflow_id": workflow["id"],
                                         "name": name, "authors": agent_ids})
        return proposed

    def coauthors(self, workflow_id: str) -> list[dict]:
        rows = self.store.query(
            """select c.*, a.name, a.agent_key from workflow_coauthors c
               join agents a on a.id = c.agent_id where c.workflow_id = ?
               order by c.created_date""",
            (workflow_id,),
        )
        return [
            {"agent_id": r["agent_id"], "name": r["name"], "agent_key": r["agent_key"],
             "role": r["role"], "verdict": r["verdict"], "note": r["note"]}
            for r in rows
        ]

    def review(self, workflow_id: str, agent_id: str, verdict: str, note: str = "") -> dict:
        """a co-author signing off on someone else's draft. recorded, not decisive."""
        if verdict not in {"approved", "changes_requested"}:
            raise ValidationError("verdict must be 'approved' or 'changes_requested'")
        workflow = self.get(workflow_id)
        if not any(c["agent_id"] == agent_id for c in workflow["coauthors"]):
            raise ValidationError("that agent is not a co-author of this workflow")
        with self.store.tx() as c:
            c.execute(
                """update workflow_coauthors set verdict = ?, note = ?
                   where workflow_id = ? and agent_id = ?""",
                (verdict, note, workflow_id, agent_id),
            )
        refreshed = self.get(workflow_id)
        refreshed["ready_for_approval"] = self.ready_for_approval(refreshed)
        return refreshed

    def ready_for_approval(self, workflow: dict) -> bool:
        verdicts = [c["verdict"] for c in workflow.get("coauthors") or []]
        return bool(verdicts) and all(v in {"approved", "pending"} for v in verdicts) and any(
            v == "approved" for v in verdicts
        )

    def approve(self, workflow_id: str, actor: str = "owner", note: str = "") -> dict:
        workflow = self.get(workflow_id)
        if workflow["status"] == "active":
            return workflow
        if workflow["status"] not in {"draft", "proposed"}:
            raise ValidationError(f"cannot approve a workflow that is {workflow['status']}")
        with self.store.tx() as c:
            c.execute(
                """update workflows set status = 'active', approved_by = ?, approved_date = ?,
                       updated_date = ? where id = ?""",
                (actor, now(), now(), workflow_id),
            )
        approved = self.get(workflow_id)
        proposer = approved.get("proposed_by_agent_id")
        if self.audit is not None and proposer:
            self.audit.record(
                team_id=approved["team_id"], agent_id=proposer, action="workflow.approved",
                actor_kind="human" if not str(actor).startswith("agent_") else "agent",
                actor_id=actor, target_kind="workflow", target_id=workflow_id,
                payload={"name": approved["name"], "note": note},
            )
        self._emit("workflow.approved", {"team_id": approved["team_id"],
                                         "workflow_id": workflow_id, "name": approved["name"],
                                         "actor": actor})
        return approved

    def reject(self, workflow_id: str, actor: str = "owner", note: str = "") -> dict:
        workflow = self.get(workflow_id)
        with self.store.tx() as c:
            c.execute(
                "update workflows set status = 'draft', updated_date = ? where id = ?",
                (now(), workflow_id),
            )
        rejected = self.get(workflow_id)
        proposer = rejected.get("proposed_by_agent_id")
        if self.audit is not None and proposer:
            self.audit.record(
                team_id=rejected["team_id"], agent_id=proposer, action="workflow.rejected",
                actor_kind="human", actor_id=actor, target_kind="workflow",
                target_id=workflow_id, payload={"note": note, "name": workflow["name"]},
            )
        return rejected

    def publish(self, workflow_id: str, spec: str = "", kind: str = "cron",
                schedule_name: str | None = None) -> dict:
        """make it run itself. this is the step that turns an approved draft into a
        standing job: one schedule row, and the beat does the rest.

        with no spec, the workflow is only marked runnable (status stays active and a
        schedule is created only if one is already bound).
        """
        workflow = self.get(workflow_id)
        if workflow["status"] != "active":
            raise ValidationError("approve the workflow before it can run on a schedule")
        if not spec:
            return {"workflow": workflow, "schedule": None}
        if self.scheduler is None:
            raise ValidationError("publishing a schedule needs the scheduler")
        name = schedule_name or f"workflow:{workflow['name']}"
        template = {"kind": "workflow", "workflow_id": workflow_id}
        existing = next(
            (s for s in self.scheduler.list(workflow["team_id"]) if s["name"] == name), None
        )
        if existing:
            with self.store.tx() as c:
                c.execute(
                    "update schedules set spec = ?, task_template = ?, kind = ?, enabled = 1 where id = ?",
                    (str(spec), dumps(template), kind, existing["id"]),
                )
            schedule = self.scheduler.get(existing["id"])
        else:
            schedule = self.scheduler.create(
                workflow["team_id"], name, kind, str(spec), task_template=template
            )
        return {"workflow": workflow, "schedule": schedule}

    def _emit(self, event: str, context: dict) -> None:
        if self.events_hook:
            self.events_hook(event, context)

    # ---- execution -----------------------------------------------------
    def run(self, workflow_id: str, trigger_payload: dict | None = None) -> dict:
        workflow = self.get(workflow_id)
        if workflow["status"] != "active":
            raise ValidationError(
                f"workflow '{workflow['name']}' is {workflow['status']}; approve it before running"
            )
        definition = workflow["definition"]
        run_id = new_id("wfr")
        state: dict[str, Any] = {
            "run_id": run_id,
            "trigger": trigger_payload or {},
            "nodes": {},
            "outputs": {},
            "context": {},
            "skipped": [],
            "status": "running",
        }
        with self.store.tx() as c:
            c.execute(
                "insert into workflow_runs (id, workflow_id, status, state, created_date) values (?,?,?,?,?)",
                (run_id, workflow_id, "running", dumps(state), now()),
            )
        self._advance(workflow, run_id, state)
        return self.get_run(run_id)

    def _advance(self, workflow: dict, run_id: str, state: dict) -> None:
        nodes = {n["id"]: n for n in workflow["definition"]["nodes"]}
        while True:
            ready = [
                node for node in nodes.values()
                if node["id"] not in state["nodes"]
                and node["id"] not in state["skipped"]
                and all(self._dep_done(dep, state) for dep in node.get("depends_on") or [])
            ]
            if not ready:
                break
            for node in ready:
                outcome = self._run_node(workflow, node, state)
                state["nodes"][node["id"]] = outcome
                if outcome.get("status") == "waiting":
                    state["status"] = "waiting"
                    self._save(run_id, "waiting", state)
                    return
                if outcome.get("status") == "failed":
                    state["status"] = "failed"
                    self._save(run_id, "failed", state)
                    return
                if node["type"] == "branch":
                    chosen = outcome.get("chosen") or []
                    for target in node.get("targets") or []:
                        if target not in chosen and target not in state["nodes"]:
                            state["skipped"].append(target)
            self._save(run_id, "running", state)
        failed = [nid for nid, o in state["nodes"].items() if o.get("status") == "failed"]
        waiting = [nid for nid, o in state["nodes"].items() if o.get("status") == "waiting"]
        state["status"] = "failed" if failed else ("waiting" if waiting else "succeeded")
        self._save(run_id, state["status"], state)

    def _dep_done(self, dep: str, state: dict) -> bool:
        if dep in state["skipped"]:
            return True
        return dep in state["nodes"]

    def _run_node(self, workflow: dict, node: dict, state: dict) -> dict:
        kind = node["type"]
        try:
            if kind == "agent_task":
                return self._node_agent_task(workflow, node, state)
            if kind == "shell":
                return self._node_shell(workflow, node, state)
            if kind == "set_context":
                key = node.get("key") or node["id"]
                value = self._render(node.get("value"), state)
                self.crews.put_context(workflow["team_id"], key, value, author=f"workflow:{workflow['name']}")
                state["outputs"][node["id"]] = value
                state.setdefault("context", {})[key] = value
                return {"status": "succeeded", "value": value}
            if kind == "human_gate":
                task = self.kanban.create_task(
                    team_id=workflow["team_id"],
                    title=self._render(node.get("title") or f"approve {node['id']}", state) or "",
                    description=self._render(node.get("description"), state) or "",
                    column="review" if "review" in self.kanban.columns(workflow["team_id"]) else "in_progress",
                    payload={"workflow_run": state.get("run_id"), "node": node["id"]},
                )
                outcome = {"status": "waiting", "task_id": task["id"]}
                gate = self._open_gate(workflow, node, state, task)
                if gate:
                    outcome["gate_id"] = gate["id"]
                    outcome["gate_links"] = gate.get("links", {})
                return outcome
            if kind == "n8n":
                return self._node_n8n(workflow, node, state)
            if kind == "dify":
                return self._node_dify(workflow, node, state)
            if kind == "branch":
                chosen = self._branch_targets(node, state)
                state["outputs"][node["id"]] = chosen
                return {"status": "succeeded", "chosen": chosen}
            return {"status": "failed", "error": f"unhandled node type {kind}"}
        except Exception as exc:  # node failure must not kill the run silently
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


    def _open_gate(self, workflow: dict, node: dict, state: dict, task: dict) -> dict | None:
        """the gate is a link, not a dashboard visit.

        the card in `review` is the record; the gate is the question, and the links
        in it are the answer. if a crew has a channel with humans in it, the question
        is posted there too, because a link nobody sees is a run nobody finishes.
        """
        if self.gates is None:
            return None
        title = self._render(node.get("title") or f"approve {node['id']}", state) or ""
        description = self._render(node.get("description") or "", state) or ""
        gate = self.gates.open(
            team_id=workflow["team_id"],
            title=str(title),
            prompt=str(description),
            run_id=str(state.get("run_id") or ""),
            node_id=node["id"],
            options=node.get("options") or ["approve", "reject"],
            channel_id=node.get("channel_id"),
        )
        channel_id = node.get("channel_id") or self._first_channel(workflow["team_id"])
        if channel_id:
            links = gate.get("links", {})
            lines = [f"needs a decision: {title}"]
            for decision, url in links.items():
                lines.append(f"{decision}: {url}")
            lines.append("one tap, no login. the link works once.")
            try:
                self.gates.channels.post(
                    channel_id,
                    "\n".join(lines),
                    author_kind="system",
                    task_id=task["id"],
                )
            except Exception:  # noqa: BLE001 - a delivery failure is not a gate failure
                pass
        return gate

    def _first_channel(self, team_id: str) -> str | None:
        if self.gates is None or self.gates.channels is None:
            return None
        try:
            channels = self.gates.channels.list(team_id)
        except Exception:  # noqa: BLE001
            return None
        return channels[0]["id"] if channels else None

    def _node_n8n(self, workflow: dict, node: dict, state: dict) -> dict:
        """delegate one step to a workflow that already lives in n8n."""
        path = self._render(node.get("path") or node.get("webhook"), state)
        payload = self._render(node.get("payload"), state) or {}
        if not isinstance(payload, dict):
            payload = {"input": payload}
        result = self.services.n8n.trigger(
            str(path), payload, test=bool(node.get("test"))
        )
        state["outputs"][node["id"]] = result.get("result")
        return {"status": "succeeded", "output": result.get("result"), "webhook": result.get("webhook")}

    def _node_dify(self, workflow: dict, node: dict, state: dict) -> dict:
        """delegate one step to a workflow that already lives in dify."""
        inputs = self._render(node.get("inputs"), state) or {}
        if not isinstance(inputs, dict):
            inputs = {"input": inputs}
        data = self.services.dify.run_workflow(
            inputs,
            user=self._render(node.get("user"), state) or None,
            response_mode=str(node.get("response_mode") or "blocking"),
        )
        state["outputs"][node["id"]] = data.get("outputs") if isinstance(data, dict) else data
        failed = str((data or {}).get("status", "")).lower() in {"failed", "stopped"}
        outcome = {"status": "failed" if failed else "succeeded", "output": data}
        if failed:
            outcome["error"] = str((data or {}).get("error") or "dify reported failure")
        return outcome

    def _node_agent_task(self, workflow: dict, node: dict, state: dict) -> dict:
        agent_name = node.get("agent")
        agents = self.crews.list_agents(workflow["team_id"])
        agent_id = None
        if agent_name:
            match = [a for a in agents if a["name"] == agent_name or a["id"] == agent_name]
            if not match:
                return {"status": "failed", "error": f"agent '{agent_name}' not in crew"}
            agent_id = match[0]["id"]
        title = self._render(node.get("title") or node.get("instruction"), state) or node["id"]
        task = self.kanban.create_task(
            team_id=workflow["team_id"],
            title=title[:200],
            description=self._render(node.get("instruction"), state) or "",
            column="ready",
            assignee_agent_id=agent_id,
            required_skills=list(node.get("skills") or []),
            isolation=node.get("isolation", "agent"),
        )
        outcome = self.runner.dispatch(task["id"], max_turns=int(node.get("max_turns", 6)))
        state["outputs"][node["id"]] = outcome.get("result", {})
        return {"status": "succeeded", "task_id": task["id"], "run_id": outcome["run_id"],
                "result": outcome.get("result", {})}

    def _node_shell(self, workflow: dict, node: dict, state: dict) -> dict:
        agent_name = node.get("agent")
        agents = self.crews.list_agents(workflow["team_id"])
        agent = next((a for a in agents if a["name"] == agent_name or a["id"] == agent_name), None)
        if agent is None:
            return {"status": "failed", "error": f"shell node needs a valid agent, got '{agent_name}'"}
        computer = self.crews.ensure_computer(agent["id"])
        provider = self.runner.registry.get(computer["provider"])
        handle = Handle.from_dict(computer["handle"])
        command = self._render(node["command"], state) or ""
        outcome = provider.exec(handle, command, timeout=int(node.get("timeout", 300)))
        state["outputs"][node["id"]] = outcome.stdout
        return {"status": "succeeded" if outcome.ok else "failed", **outcome.to_dict()}

    def _branch_targets(self, node: dict, state: dict) -> list[str]:
        targets = node.get("targets") or []
        for case in node.get("cases") or []:
            field = (case.get("field") or "").strip()
            value = self._lookup(field, state)
            if "equals" in case and value == case["equals"]:
                return [case["target"]] if case.get("target") in targets else targets
            if "truthy" in case and bool(value) == bool(case["truthy"]):
                return [case["target"]] if case.get("target") in targets else targets
        return targets

    # ---- helpers -------------------------------------------------------
    def _lookup(self, path: str, state: dict) -> Any:
        """resolve `a.b.c` against node outputs, trigger payload, or crew context."""
        if not path:
            return None
        root, _, rest = path.partition(".")
        if root in state["nodes"]:
            value: Any = state["nodes"][root]
        elif root in state.get("context", {}):
            value = state["context"][root]
        elif root in state:
            value = state[root]
        else:
            return None
        for part in [p for p in rest.split(".") if p]:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None
        return value

    def _render(self, template: Any, state: dict) -> Any:
        if not isinstance(template, str):
            return template

        def repl(match: re.Match) -> str:
            value = self._lookup(match.group(1), state)
            return "" if value is None else str(value)

        return TEMPLATE.sub(repl, template)

    def _save(self, run_id: str, status: str, state: dict) -> None:
        terminal = status in {"succeeded", "failed"}
        with self.store.tx() as c:
            c.execute(
                "update workflow_runs set status = ?, state = ?, ended_at = ? where id = ?",
                (status, dumps(state), now() if terminal else None, run_id),
            )
        if terminal and self.events_hook:
            row = self.store.one(
                """select r.id, w.id as workflow_id, w.name, w.team_id
                   from workflow_runs r join workflows w on w.id = r.workflow_id
                   where r.id = ?""",
                (run_id,),
            )
            if row:
                self.events_hook(
                    "workflow.finished",
                    {"team_id": row["team_id"], "run_id": row["id"],
                     "workflow_id": row["workflow_id"], "name": row["name"],
                     "status": status, "state": state},
                )
                if status == "failed":
                    failed = [nid for nid, out in (state.get("nodes") or {}).items()
                              if (out or {}).get("status") == "failed"]
                    first = (state.get("nodes") or {}).get(failed[0], {}) if failed else {}
                    self.events_hook(
                        "run.failed",
                        {"team_id": row["team_id"], "workflow_id": row["workflow_id"],
                         "run_id": row["id"], "task_id": "", "node_id": failed[0] if failed else "",
                         "error": str((first or {}).get("error") or f"{row['name']} failed")[:300]},
                    )

    def get_run(self, run_id: str) -> dict:
        row = self.store.one("select * from workflow_runs where id = ?", (run_id,))
        if not row:
            raise NotFound(f"workflow run {run_id} not found")
        return {
            "id": row["id"], "workflow_id": row["workflow_id"], "status": row["status"],
            "state": loads(row["state"], {}), "created_date": row["created_date"],
            "ended_at": row["ended_at"],
        }

    def resume(self, run_id: str, node_id: str, decision: str = "approved", note: str = "") -> dict:
        """a human_gate was decided; finish that node and continue the dag."""
        run = self.get_run(run_id)
        if run["status"] != "waiting":
            raise ValidationError(f"workflow run is {run['status']}, not waiting")
        workflow = self.get(run["workflow_id"])
        state = run["state"]
        node = state["nodes"].get(node_id)
        if not node or node.get("status") != "waiting":
            raise ValidationError(f"node '{node_id}' is not waiting")
        if decision not in {"approved", "rejected"}:
            raise ValidationError("decision must be 'approved' or 'rejected'")
        if node.get("task_id"):
            self.kanban.move(node["task_id"], "done", "owner", note=note)
        if self.gates is not None and node.get("gate_id"):
            try:
                self.gates.decide(node["gate_id"], decision=decision, actor="owner", note=note)
            except Exception:  # noqa: BLE001 - already decided via the link is the normal case
                pass
        state["nodes"][node_id] = {"status": "succeeded" if decision == "approved" else "failed",
                                   "decision": decision, "note": note,
                                   "task_id": node.get("task_id")}
        state["status"] = "running"
        self._advance(workflow, run_id, state)
        return self.get_run(run_id)
