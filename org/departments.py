"""departments: the hierarchy, kept explicit.

a swarm of agents that all talk to each other is a group chat with extra steps. the
lesson worth taking from paperclip's org chart is that a **team has one accountable
head**: a department has a coordinator, members report to it, and departments nest.
routing a task to a department means routing it to that coordinator, which then
delegates. one throat to choke, one person whose board the work lands on.

this is the hmas half of the product: hierarchy for assignment and escalation, while
the channels stay flat for conversation. the two are separate on purpose.
"""

from __future__ import annotations

from core.db import Store, new_id, now
from core.errors import Conflict, NotFound, ValidationError


def slugify(value: str) -> str:
    from agents.crews import slugify as crew_slugify

    return crew_slugify(value)


class DepartmentService:
    def __init__(self, store: Store, kanban=None, a2a=None):
        self.store = store
        self.kanban = kanban
        self.a2a = a2a
        self.events_hook = None

    # ---- departments ---------------------------------------------------
    def create(
        self,
        team_id: str,
        name: str,
        coordinator_agent_id: str | None = None,
        parent_id: str | None = None,
        purpose: str = "",
        members: list[str] | None = None,
        slug: str | None = None,
    ) -> dict:
        if not name:
            raise ValidationError("department name is required")
        if not self.store.one("select id from teams where id = ?", (team_id,)):
            raise NotFound(f"crew {team_id} not found")
        if parent_id:
            parent = self.get(parent_id)
            if parent["team_id"] != team_id:
                raise ValidationError("a department's parent must be in the same crew")
        dept_slug = slugify(slug or name)
        if self.store.one(
            "select id from departments where team_id = ? and slug = ?", (team_id, dept_slug)
        ):
            raise Conflict(f"department '{dept_slug}' already exists in {team_id}")
        if coordinator_agent_id:
            self._require_agent(team_id, coordinator_agent_id)
        dept_id = new_id("dept")
        ts = now()
        with self.store.tx() as c:
            c.execute(
                """insert into departments (id, team_id, parent_id, name, slug, purpose,
                       coordinator_agent_id, created_date, updated_date)
                   values (?,?,?,?,?,?,?,?,?)""",
                (dept_id, team_id, parent_id, name, dept_slug, purpose,
                 coordinator_agent_id, ts, ts),
            )
        for agent_id in [*(members or []), *([coordinator_agent_id] if coordinator_agent_id else [])]:
            self.add_member(dept_id, agent_id)
        # the head also gets a channel for its department: hierarchy for routing,
        # a room for the conversation
        return self.get(dept_id)

    def get(self, department_id: str) -> dict:
        row = self.store.one("select * from departments where id = ?", (department_id,))
        if not row:
            raise NotFound(f"department {department_id} not found")
        return self._department(row)

    def list(self, team_id: str) -> list[dict]:
        rows = self.store.query(
            "select * from departments where team_id = ? order by created_date", (team_id,)
        )
        return [self._department(row) for row in rows]

    def update(self, department_id: str, **fields) -> dict:
        allowed = {"name", "purpose", "parent_id", "coordinator_agent_id"}
        patch = {k: v for k, v in fields.items() if k in allowed}
        if "parent_id" in patch and patch["parent_id"]:
            self._assert_no_cycle(department_id, patch["parent_id"])
        if "coordinator_agent_id" in patch and patch["coordinator_agent_id"]:
            dept = self.get(department_id)
            self._require_agent(dept["team_id"], patch["coordinator_agent_id"])
            self.add_member(department_id, patch["coordinator_agent_id"])
        if not patch:
            return self.get(department_id)
        sets = ", ".join(f"{k} = ?" for k in patch)
        with self.store.tx() as c:
            c.execute(
                f"update departments set {sets}, updated_date = ? where id = ?",
                (*patch.values(), now(), department_id),
            )
        return self.get(department_id)

    def _department(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "parent_id": row["parent_id"],
            "name": row["name"],
            "slug": row["slug"],
            "purpose": row["purpose"],
            "coordinator_agent_id": row["coordinator_agent_id"],
            "members": self.members(row["id"]),
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    # ---- members -------------------------------------------------------
    def add_member(self, department_id: str, agent_id: str) -> dict:
        dept = self.get(department_id)
        self._require_agent(dept["team_id"], agent_id)
        with self.store.tx() as c:
            c.execute(
                """insert or ignore into department_members (department_id, agent_id, created_date)
                   values (?,?,?)""",
                (department_id, agent_id, now()),
            )
        return self.get(department_id)

    def remove_member(self, department_id: str, agent_id: str) -> dict:
        dept = self.get(department_id)
        if dept["coordinator_agent_id"] == agent_id:
            raise ValidationError("the coordinator cannot be removed; set another one first")
        with self.store.tx() as c:
            c.execute(
                "delete from department_members where department_id = ? and agent_id = ?",
                (department_id, agent_id),
            )
        return self.get(department_id)

    def members(self, department_id: str) -> list[dict]:
        rows = self.store.query(
            """select m.agent_id, a.name, a.role from department_members m
               join agents a on a.id = m.agent_id
               where m.department_id = ? order by a.name""",
            (department_id,),
        )
        return [{"agent_id": r["agent_id"], "name": r["name"], "role": r["role"]} for r in rows]

    def _require_agent(self, team_id: str, agent_id: str) -> None:
        row = self.store.one("select team_id from agents where id = ?", (agent_id,))
        if not row:
            raise NotFound(f"agent {agent_id} not found")
        if row["team_id"] != team_id:
            raise ValidationError("agent belongs to a different crew")

    def _assert_no_cycle(self, department_id: str, parent_id: str) -> None:
        seen = {department_id}
        cursor: str | None = parent_id
        while cursor:
            if cursor in seen:
                raise ValidationError("that parent would create a reporting cycle")
            seen.add(cursor)
            row = self.store.one("select parent_id from departments where id = ?", (cursor,))
            cursor = row["parent_id"] if row else None

    # ---- the org chart -------------------------------------------------
    def department_for_agent(self, agent_id: str) -> dict | None:
        row = self.store.one(
            """select d.* from departments d join department_members m on m.department_id = d.id
               where m.agent_id = ? order by d.created_date limit 1""",
            (agent_id,),
        )
        return self._department(row) if row else None

    def chain(self, agent_id: str) -> list[dict]:
        """reporting line, from the agent's department up to the root."""
        chain: list[dict] = []
        current = self.department_for_agent(agent_id)
        seen: set[str] = set()
        while current and current["id"] not in seen:
            seen.add(current["id"])
            chain.append(current)
            parent_id = current["parent_id"]
            if not parent_id:
                break
            row = self.store.one("select * from departments where id = ?", (parent_id,))
            current = self._department(row) if row else None
        return chain

    def tree(self, team_id: str) -> dict:
        """nested org chart. an unknown parent is treated as a root so a half edited
        chart still renders instead of vanishing."""
        departments = self.list(team_id)
        nodes = {d["id"]: {**d, "children": []} for d in departments}
        roots = []
        for dept in departments:
            node = nodes[dept["id"]]
            parent = nodes.get(dept["parent_id"] or "")
            (parent["children"] if parent else roots).append(node)
        return {
            "team_id": team_id,
            "departments": len(departments),
            "roots": roots,
            "unassigned": self.unassigned_agents(team_id),
        }

    def unassigned_agents(self, team_id: str) -> list[dict]:
        rows = self.store.query(
            """select a.id, a.name, a.role from agents a
               where a.team_id = ?
                 and not exists (select 1 from department_members m where m.agent_id = a.id)
               order by a.name""",
            (team_id,),
        )
        return [{"agent_id": r["id"], "name": r["name"], "role": r["role"]} for r in rows]

    # ---- routing -------------------------------------------------------
    def route(self, task_id: str, department_id: str | None = None) -> dict:
        """put the card on an accountable desk: the coordinator's.

        with no department given, the card's own assignee decides, so `route` can be
        used to repair a task that was assigned to a member by mistake.
        """
        if self.kanban is None:
            raise ValidationError("routing needs a kanban service")
        task = self.kanban.get_task(task_id)
        department = self.get(department_id) if department_id else self.department_for_agent(
            task["assignee_agent_id"] or ""
        )
        if not department:
            raise ValidationError(
                "no department given and the task's assignee is not in one"
            )
        if department["team_id"] != task["team_id"]:
            raise ValidationError("task and department are in different crews")
        coordinator = department["coordinator_agent_id"]
        if not coordinator:
            raise ValidationError(f"department '{department['slug']}' has no coordinator")
        updated = self.kanban.assign(task_id, coordinator, actor="department")
        if self.events_hook:
            self.events_hook(
                "task.routed",
                {"task": updated, "department": department, "team_id": task["team_id"]},
            )
        return {"task": updated, "department": department, "coordinator_agent_id": coordinator}

    def escalate(self, agent_id: str, body: str, task_id: str | None = None) -> dict:
        """a member pushing a decision up its own reporting line, as an a2a message
        that still goes through the review gate."""
        if self.a2a is None:
            raise ValidationError("escalation needs the a2a service")
        chain = self.chain(agent_id)
        if not chain:
            raise ValidationError("that agent is not in a department")
        current = chain[0]
        parent = chain[1] if len(chain) > 1 else None
        target = (parent or current)["coordinator_agent_id"]
        if not target or target == agent_id:
            raise ValidationError("nowhere to escalate: no coordinator above this agent")
        message = self.a2a.send(
            team_id=current["team_id"],
            from_agent_id=agent_id,
            to_agent_id=target,
            body=body,
            task_id=task_id,
            kind="blocker",
        )
        return {"message": message, "to_department": (parent or current)["slug"]}
