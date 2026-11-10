"""shifts: coverage, and a handoff that carries the work.

the metaphor is the one that already works in a coffee shop: someone is on the bar,
someone clocks in, the outgoing person briefs the incoming one, and only then does
the first one go home. the brief is not a vibe, it is a bundle.

two mechanics worth stating because they are the whole design:

**rotation is arithmetic, not a scheduled row.** `slot_index = (now - anchor) // slot`
and the person on duty is `rotation[slot_index % len(rotation)]`. a duplicated beat
cannot skew it and a missed beat cannot skip somebody's shift, because nothing about
coverage is stored except the plan itself.

**a handoff is accepted, not assumed.** the incoming agent (or the owner) accepts it,
and accepting transfers the open cards to them, in one transaction, with an audit
row. a shift nobody picked up stays `open` and shows up in the incoming agent's
briefing instead of silently expiring.

modes:
  async  one owner per slot, a handoff at every boundary (the default)
  sync   the whole rotation works the slot together; handoffs are still recorded but
         nobody has to accept them, because nobody is leaving
"""

from __future__ import annotations

import math
from typing import Any

from clawcrew.db import Store, dumps, loads, new_id, now
from clawcrew.errors import Conflict, NotAllowed, NotFound, ValidationError

MODES = {"async", "sync"}
STATUSES = {"open", "accepted", "declined", "expired"}
OPEN_COLUMNS = {"backlog", "ready", "in_progress", "review", "blocked"}


class ShiftService:
    def __init__(self, store: Store, crews, kanban, channels=None, a2a=None, audit=None):
        self.store = store
        self.crews = crews
        self.kanban = kanban
        self.channels = channels
        self.a2a = a2a
        self.audit = audit
        self.events_hook = None

    # ---- plans ---------------------------------------------------------
    def create_plan(
        self,
        team_id: str,
        name: str,
        rotation: list[str],
        slot_minutes: int = 240,
        anchor_at: float | None = None,
        mode: str = "async",
        channel_id: str | None = None,
        timezone: str = "UTC",
        enabled: bool = True,
    ) -> dict:
        if not name:
            raise ValidationError("shift plan name is required")
        if mode not in MODES:
            raise ValidationError(f"mode must be one of {sorted(MODES)}")
        if not rotation:
            raise ValidationError("a rotation needs at least one agent")
        if len(set(rotation)) != len(rotation):
            raise ValidationError("rotation entries must be unique")
        if slot_minutes < 5 or slot_minutes > 7 * 24 * 60:
            raise ValidationError("slot_minutes must be between 5 and 10080")
        team = self.crews.get_team(team_id)
        for agent_id in rotation:
            agent = self.crews.get_agent(agent_id)
            if agent["team_id"] != team["id"]:
                raise ValidationError("every rotation member must be in the same crew")
        if channel_id and self.channels is not None:
            channel = self.channels.get(channel_id)
            if channel["team_id"] != team_id:
                raise ValidationError("shift channel must belong to the same crew")
        plan_id = new_id("shift")
        ts = now()
        with self.store.tx() as c:
            try:
                c.execute(
                    """insert into shift_plans
                       (id, team_id, channel_id, name, slot_minutes, anchor_at, rotation, mode,
                        timezone, enabled, created_date, updated_date)
                       values (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        plan_id, team_id, channel_id, name, int(slot_minutes),
                        float(anchor_at if anchor_at is not None else ts),
                        dumps(rotation), mode, timezone, 1 if enabled else 0, ts, ts,
                    ),
                )
            except Exception as exc:  # unique (team_id, name)
                raise Conflict(f"a shift plan named '{name}' already exists in this crew") from exc
        return self.get_plan(plan_id)

    def get_plan(self, plan_id: str) -> dict:
        row = self.store.one("select * from shift_plans where id = ?", (plan_id,))
        if not row:
            raise NotFound(f"shift plan {plan_id} not found")
        return self._plan(row)

    def list_plans(self, team_id: str) -> list[dict]:
        rows = self.store.query(
            "select * from shift_plans where team_id = ? order by created_date", (team_id,)
        )
        return [self._plan(r) for r in rows]

    def update_plan(self, plan_id: str, **fields) -> dict:
        allowed = {"name", "slot_minutes", "anchor_at", "rotation", "mode", "channel_id",
                   "timezone", "enabled"}
        patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "mode" in patch and patch["mode"] not in MODES:
            raise ValidationError(f"mode must be one of {sorted(MODES)}")
        if "rotation" in patch:
            if not patch["rotation"]:
                raise ValidationError("a rotation needs at least one agent")
            plan = self.get_plan(plan_id)
            for agent_id in patch["rotation"]:
                if self.crews.get_agent(agent_id)["team_id"] != plan["team_id"]:
                    raise ValidationError("every rotation member must be in the same crew")
            patch["rotation"] = dumps(patch["rotation"])
        if not patch:
            return self.get_plan(plan_id)
        if "enabled" in patch:
            patch["enabled"] = 1 if patch["enabled"] else 0
        sets = ", ".join(f"{k} = ?" for k in patch)
        with self.store.tx() as c:
            c.execute(
                f"update shift_plans set {sets}, updated_date = ? where id = ?",
                (*patch.values(), now(), plan_id),
            )
        return self.get_plan(plan_id)

    def delete_plan(self, plan_id: str) -> dict:
        plan = self.get_plan(plan_id)
        with self.store.tx() as c:
            c.execute("delete from shift_plans where id = ?", (plan_id,))
        return plan

    def _plan(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "channel_id": row["channel_id"],
            "name": row["name"],
            "slot_minutes": row["slot_minutes"],
            "anchor_at": row["anchor_at"],
            "rotation": loads(row["rotation"], []),
            "mode": row["mode"],
            "timezone": row["timezone"],
            "enabled": bool(row["enabled"]),
            "created_date": row["created_date"],
            "updated_date": row["updated_date"],
        }

    # ---- coverage ------------------------------------------------------
    def slot_at(self, plan: dict, at: float | None = None) -> int:
        at = now() if at is None else at
        elapsed = at - float(plan["anchor_at"])
        return max(0, int(math.floor(elapsed / (plan["slot_minutes"] * 60))))

    def slot_window(self, plan: dict, slot_index: int) -> tuple[float, float]:
        length = plan["slot_minutes"] * 60
        start = float(plan["anchor_at"]) + slot_index * length
        return start, start + length

    def owner_of_slot(self, plan: dict, slot_index: int) -> str:
        rotation = plan["rotation"]
        return rotation[slot_index % len(rotation)]

    def on_duty(self, plan_id: str, at: float | None = None) -> dict:
        plan = self.get_plan(plan_id)
        slot = self.slot_at(plan, at)
        start, end = self.slot_window(plan, slot)
        owner = self.owner_of_slot(plan, slot)
        agent = self.crews.get_agent(owner)
        return {
            "plan_id": plan["id"],
            "plan": plan["name"],
            "mode": plan["mode"],
            "slot_index": slot,
            "slot_start": start,
            "slot_end": end,
            "agent": {"agent_id": agent["id"], "agent_key": agent["agent_key"],
                      "name": agent["name"], "role": agent["role"]},
            "on_duty": [owner] if plan["mode"] == "async" else list(plan["rotation"]),
        }

    def schedule(self, plan_id: str, slots: int = 8, at: float | None = None) -> list[dict]:
        """upcoming coverage, so a UI can draw the rota instead of computing it."""
        plan = self.get_plan(plan_id)
        here = self.slot_at(plan, at)
        rows = []
        for offset in range(max(1, min(slots, 96))):
            index = here + offset
            start, end = self.slot_window(plan, index)
            owner = self.owner_of_slot(plan, index)
            agent = self.crews.get_agent(owner)
            rows.append(
                {
                    "slot_index": index,
                    "slot_start": start,
                    "slot_end": end,
                    "agent_id": owner,
                    "agent_key": agent["agent_key"],
                    "name": agent["name"],
                    "current": offset == 0,
                }
            )
        return rows

    def coverage(self, team_id: str, at: float | None = None) -> dict:
        plans = [p for p in self.list_plans(team_id) if p["enabled"]]
        return {
            "team_id": team_id,
            "now": now() if at is None else at,
            "plans": [
                {
                    "plan": p["name"],
                    "plan_id": p["id"],
                    "mode": p["mode"],
                    "channel_id": p["channel_id"],
                    **{k: v for k, v in self.on_duty(p["id"], at).items()
                       if k in {"slot_index", "slot_start", "slot_end", "agent", "on_duty"}},
                }
                for p in plans
            ],
            "open_handoffs": len(self.handoffs(team_id, status="open")),
        }

    # ---- handoffs ------------------------------------------------------
    def roll(self, plan_id: str, at: float | None = None) -> dict:
        """make sure the handoff for the current slot exists, then return it.

        idempotent by the (plan, slot) unique index: calling it from a cron, from a
        page load and from a webhook converges on one row.
        """
        plan = self.get_plan(plan_id)
        slot = self.slot_at(plan, at)
        existing = self.store.one(
            "select id from handoffs where plan_id = ? and slot_index = ?", (plan_id, slot)
        )
        if existing:
            return self.get_handoff(existing["id"])
        to_agent = self.owner_of_slot(plan, slot)
        from_agent = self.owner_of_slot(plan, slot - 1) if slot > 0 else None
        start, end = self.slot_window(plan, slot)
        bundle = self.build_bundle(plan, slot, to_agent)
        handoff_id = new_id("hand")
        with self.store.tx() as c:
            c.execute(
                """insert into handoffs
                   (id, team_id, plan_id, channel_id, slot_index, from_agent_id, to_agent_id,
                    task_ids, bundle, status, note, created_date, accepted_date, expires_at)
                   values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    handoff_id, plan["team_id"], plan["id"], plan["channel_id"], slot,
                    from_agent, to_agent, dumps(bundle["task_ids"]), dumps(bundle),
                    "accepted" if plan["mode"] == "sync" else "open", "",
                    now(), now() if plan["mode"] == "sync" else None, end,
                ),
            )
        handoff = self.get_handoff(handoff_id)
        if self.audit is not None:
            self.audit.record(
                team_id=plan["team_id"],
                agent_id=to_agent,
                action="handoff.opened",
                actor_kind="system",
                actor_id="shift",
                target_kind="handoff",
                target_id=handoff_id,
                payload={"plan": plan["name"], "slot": slot, "from": from_agent,
                         "cards": len(bundle["task_ids"])},
            )
        if self.events_hook:
            self.events_hook(
                "shift.rolled",
                {"team_id": plan["team_id"], "handoff": handoff, "plan": plan},
            )
        return handoff

    def get_handoff(self, handoff_id: str) -> dict:
        row = self.store.one("select * from handoffs where id = ?", (handoff_id,))
        if not row:
            raise NotFound(f"handoff {handoff_id} not found")
        return self._handoff(row)

    def handoffs(
        self, team_id: str, status: str | None = None, limit: int = 50
    ) -> list[dict]:
        if status:
            if status not in STATUSES:
                raise ValidationError(f"status must be one of {sorted(STATUSES)}")
            rows = self.store.query(
                """select * from handoffs where team_id = ? and status = ?
                   order by created_date desc limit ?""",
                (team_id, status, limit),
            )
        else:
            rows = self.store.query(
                "select * from handoffs where team_id = ? order by created_date desc limit ?",
                (team_id, limit),
            )
        return [self._handoff(r) for r in rows]

    def _handoff(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "plan_id": row["plan_id"],
            "channel_id": row["channel_id"],
            "slot_index": row["slot_index"],
            "from_agent_id": row["from_agent_id"],
            "to_agent_id": row["to_agent_id"],
            "task_ids": loads(row["task_ids"], []),
            "bundle": loads(row["bundle"], {}),
            "status": row["status"],
            "note": row["note"],
            "created_date": row["created_date"],
            "accepted_date": row["accepted_date"],
            "expires_at": row["expires_at"],
        }

    def accept(self, handoff_id: str, agent_id: str | None = None, note: str = "") -> dict:
        """taking the shift: the cards move, the trail records it."""
        handoff = self.get_handoff(handoff_id)
        if handoff["status"] not in {"open", "accepted"}:
            raise NotAllowed(f"handoff is {handoff['status']}")
        recipient = agent_id or handoff["to_agent_id"]
        if recipient != handoff["to_agent_id"]:
            raise NotAllowed("only the incoming agent can take this shift")
        transferred = []
        for task_id in handoff["task_ids"]:
            task = self.kanban.get_task(task_id)
            if task["column"] in {"done", "cancelled"}:
                continue
            if task["assignee_agent_id"] != recipient:
                self.kanban.assign(task_id, recipient, actor=recipient)
                transferred.append(task_id)
        with self.store.tx() as c:
            c.execute(
                "update handoffs set status = 'accepted', accepted_date = ?, note = ? where id = ?",
                (now(), note or handoff["note"], handoff_id),
            )
        if self.audit is not None:
            self.audit.record(
                team_id=handoff["team_id"],
                agent_id=recipient,
                action="handoff.accepted",
                actor_kind="agent",
                actor_id=recipient,
                target_kind="handoff",
                target_id=handoff_id,
                payload={"slot": handoff["slot_index"], "transferred": transferred,
                         "from": handoff["from_agent_id"]},
            )
        accepted = self.get_handoff(handoff_id)
        accepted["transferred"] = transferred
        return accepted

    def decline(self, handoff_id: str, note: str = "") -> dict:
        handoff = self.get_handoff(handoff_id)
        if handoff["status"] != "open":
            raise NotAllowed(f"handoff is {handoff['status']}")
        with self.store.tx() as c:
            c.execute(
                "update handoffs set status = 'declined', note = ? where id = ?",
                (note, handoff_id),
            )
        if self.audit is not None:
            self.audit.record(
                team_id=handoff["team_id"],
                agent_id=handoff["to_agent_id"],
                action="handoff.declined",
                actor_kind="agent",
                actor_id=handoff["to_agent_id"],
                target_kind="handoff",
                target_id=handoff_id,
                payload={"note": note, "slot": handoff["slot_index"]},
            )
        return self.get_handoff(handoff_id)

    # ---- the bundle ----------------------------------------------------
    def build_bundle(self, plan: dict, slot: int, to_agent_id: str) -> dict:
        """what the incoming agent needs to be useful in the first minute: the cards
        in flight, the ones stuck, what is waiting on a human, and the room's last
        words. built from live rows, never from a stored summary that can rot."""
        team_id = plan["team_id"]
        rotation = plan["rotation"]
        outgoing = [a for a in rotation if a != to_agent_id]
        from_agent = self.owner_of_slot(plan, slot - 1) if slot > 0 else None
        cards = self.kanban.list_tasks(team_id, limit=500, channel_id=plan["channel_id"])
        # what travels: the outgoing agent's open cards, anything already waiting on
        # the incoming one, and unassigned cards raised in the room. a handoff that
        # only carries "your own work" is not a handoff.
        carry = [
            c for c in cards
            if c["column"] in OPEN_COLUMNS
            and c["assignee_agent_id"] in (from_agent, to_agent_id, None)
        ]
        mine = [c for c in carry if c["assignee_agent_id"] == to_agent_id]
        in_flight = [
            c for c in cards
            if c["column"] in {"in_progress", "review"}
            and c["assignee_agent_id"] not in (from_agent, to_agent_id, None)
        ]
        blocked = [c for c in cards if c["column"] == "blocked"]
        thread: list[dict] = []
        if self.channels is not None and plan["channel_id"]:
            thread = self.channels.messages(plan["channel_id"], limit=10)
        pending_a2a = self.store.query(
            """select id, from_agent_id, to_agent_id, kind, body from a2a_messages
               where team_id = ? and status = 'pending' order by created_date desc limit 10""",
            (team_id,),
        )
        return {
            "slot_index": slot,
            "plan": plan["name"],
            "mode": plan["mode"],
            "to_agent_id": to_agent_id,
            "from_agent_id": from_agent,
            "shift_ends_at": self.slot_window(plan, slot)[1],
            "task_ids": [c["id"] for c in carry],
            "open_cards": [
                {"id": c["id"], "title": c["title"], "column": c["column"],
                 "channel_id": c["channel_id"], "assignee_agent_id": c["assignee_agent_id"]}
                for c in carry
            ],
            "in_flight": [
                {"id": c["id"], "title": c["title"], "column": c["column"],
                 "assignee_agent_id": c["assignee_agent_id"]}
                for c in in_flight
            ],
            "blocked": [
                {"id": c["id"], "title": c["title"], "note": c.get("review_note", "")}
                for c in blocked
            ],
            "awaiting_human": [
                {"message_id": r["id"], "from": r["from_agent_id"], "kind": r["kind"],
                 "body": r["body"]}
                for r in pending_a2a
            ],
            "room": [
                {"author_kind": m["author_kind"], "author_id": m["author_id"],
                 "body": m["body"], "created_date": m["created_date"]}
                for m in thread
            ],
            "rotation": rotation,
            "outgoing": outgoing,
            "next_steps": self._next_steps(mine, in_flight, blocked),
        }

    def _next_steps(self, mine, in_flight, blocked) -> list[str]:
        steps = []
        for card in mine:
            if card["column"] == "review":
                steps.append(f"card ready for approval: {card['title']}")
            elif card["column"] == "blocked":
                steps.append(f"unblock: {card['title']}")
        if in_flight:
            steps.append(f"{len(in_flight)} card(s) were mid-flight when the shift changed")
        if blocked:
            steps.append(f"{len(blocked)} card(s) are blocked and need a decision")
        return steps

    def briefing(self, agent_id: str) -> dict:
        """everything waiting on an agent right now, across every plan they cover."""
        agent = self.crews.get_agent(agent_id)
        plans = [p for p in self.list_plans(agent["team_id"]) if agent_id in p["rotation"]]
        open_handoffs = self.store.query(
            "select * from handoffs where to_agent_id = ? and status = 'open' order by created_date desc",
            (agent_id,),
        )
        current = []
        for plan in plans:
            duty = self.on_duty(plan["id"])
            if duty["agent"]["agent_id"] == agent_id or plan["mode"] == "sync":
                current.append({**duty, "handoff": self.roll(plan["id"])})
        return {
            "agent": {"agent_id": agent["id"], "agent_key": agent["agent_key"],
                      "name": agent["name"]},
            "on_shift": current,
            "open_handoffs": [self._handoff(r) for r in open_handoffs],
            "plans": plans,
        }
