"""triggers: when this happens, queue that.

a trigger is a row, not a plugin: `event` (what happened), `condition` (an exact
match on the event context), `action` (a queued job kind) and `payload` (the job's
payload, with `${path}` placeholders filled from the context).

that shape is on purpose. an expression language would be a second product to
maintain, and the honest v1 is: exact matches only, one action per rule, and the
queue does the rest. overnight work and "when a card lands in review, dispatch the
next step" are both just rows here.

the queue is schedule's job (or sqlite's when schedule is not configured). this module
never executes work itself, so a rule that fires twice at 3am is visible in the job
table instead of hiding in a thread.
"""

from __future__ import annotations

from typing import Any

from core.db import Store, dumps, loads, new_id, now
from core.errors import NotFound, ValidationError

# documented event names. any string is accepted (a webhook or an importer may have
# its own), but these are the ones the services actually emit.
EVENTS = (
    "task.created",
    "task.moved",
    "task.assigned",
    "task.completed",
    "task.routed",
    "channel.message",
    "a2a.message",
    "workflow.finished",
    "run.failed",
    "schedule.nightly",
)

# job kinds the worker knows how to run, plus the ones a trigger can ask for
ACTIONS = ("task", "dispatch", "workflow", "schedule", "brief", "card", "shift_roll")


class TriggerService:
    def __init__(self, store: Store, broker=None):
        self.store = store
        self.broker = broker

    # ---- rules ---------------------------------------------------------
    def create(
        self,
        team_id: str,
        name: str,
        event: str,
        action: str,
        payload: dict | None = None,
        condition: str = "",
        enabled: bool = True,
    ) -> dict:
        if not name:
            raise ValidationError("trigger name is required")
        if not event:
            raise ValidationError("trigger event is required")
        if action not in ACTIONS:
            raise ValidationError(f"action must be one of {list(ACTIONS)}")
        if not self.store.one("select id from teams where id = ?", (team_id,)):
            raise NotFound(f"crew {team_id} not found")
        if condition:
            self._parse_condition(condition)  # fail at save time, not at 3am
        trigger_id = new_id("trg")
        with self.store.tx() as c:
            c.execute(
                """insert into triggers (id, team_id, name, event, action, payload, condition,
                       enabled, fired_count, created_date)
                   values (?,?,?,?,?,?,?,?,0,?)""",
                (trigger_id, team_id, name, event, action, dumps(payload or {}),
                 condition, 1 if enabled else 0, now()),
            )
        return self.get(trigger_id)

    def get(self, trigger_id: str) -> dict:
        row = self.store.one("select * from triggers where id = ?", (trigger_id,))
        if not row:
            raise NotFound(f"trigger {trigger_id} not found")
        return self._trigger(row)

    def list(self, team_id: str) -> list[dict]:
        rows = self.store.query(
            "select * from triggers where team_id = ? order by created_date", (team_id,)
        )
        return [self._trigger(row) for row in rows]

    def set_enabled(self, trigger_id: str, enabled: bool) -> dict:
        with self.store.tx() as c:
            c.execute(
                "update triggers set enabled = ? where id = ?",
                (1 if enabled else 0, trigger_id),
            )
        return self.get(trigger_id)

    def delete(self, trigger_id: str) -> dict:
        trigger = self.get(trigger_id)
        with self.store.tx() as c:
            c.execute("delete from triggers where id = ?", (trigger_id,))
        return trigger

    def _trigger(self, row) -> dict:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "name": row["name"],
            "event": row["event"],
            "action": row["action"],
            "payload": loads(row["payload"], {}),
            "condition": row["condition"],
            "enabled": bool(row["enabled"]),
            "fired_count": row["fired_count"],
            "last_fired_at": row["last_fired_at"],
            "created_date": row["created_date"],
        }

    # ---- firing --------------------------------------------------------
    def fire(self, event: str, context: dict | None = None) -> list[dict]:
        """called by the services when something happens. enqueues one job per
        matching rule and returns what it did, so a caller can log it."""
        context = context or {}
        team_id = context.get("team_id") or (context.get("task") or {}).get("team_id") or (
            context.get("channel") or {}
        ).get("team_id")
        rows = self.store.query(
            "select * from triggers where event = ? and enabled = 1 and (team_id = ? or ? = '')",
            (event, team_id or "", team_id or ""),
        )
        fired: list[dict] = []
        for row in rows:
            trigger = self._trigger(row)
            if trigger["condition"] and not self._matches(trigger["condition"], context):
                continue
            # a rule belongs to a crew, and `schedule.nightly` fires with no crew in
            # context. render per rule with its own team, or the overnight pack's
            # `${team_id}` renders empty and every 2am job dies on validation.
            rule_context = {**context, "team_id": context.get("team_id") or trigger["team_id"]}
            payload = self._render(trigger["payload"], rule_context)
            job_id = None
            if self.broker is not None:
                job_id = self.broker.enqueue(trigger["action"], payload)
            with self.store.tx() as c:
                c.execute(
                    "update triggers set fired_count = fired_count + 1, last_fired_at = ? where id = ?",
                    (now(), trigger["id"]),
                )
                c.execute(
                    """insert into trigger_log (id, team_id, trigger_id, event, action, job_id, created_date)
                       values (?,?,?,?,?,?,?)""",
                    (new_id("tgl"), trigger["team_id"], trigger["id"], event,
                     trigger["action"], job_id or "", now()),
                )
            fired.append(
                {"trigger": trigger["id"], "event": event, "action": trigger["action"],
                 "job": job_id, "payload": payload}
            )
        return fired

    # ---- condition + template ------------------------------------------
    def _parse_condition(self, condition: str) -> list[tuple[str, str, str]]:
        """`path=value`, or `path>value` and friends.

        comparisons are here because "dispatch the cards nobody touched for two hours"
        is a condition on a number, not on a word, and exact match forced the idle check
        to live in a job branch where it could not be read or edited by the operator.
        """
        pairs: list[tuple[str, str, str]] = []
        for part in str(condition).split(","):
            part = part.strip()
            if not part:
                continue
            for op in (">=", "<=", "!=", ">", "<", "="):
                if op in part:
                    path, value = part.split(op, 1)
                    pairs.append((path.strip(), op, value.strip()))
                    break
            else:
                raise ValidationError(
                    f"condition '{part}' must look like path=value or path>number")
        if not pairs:
            raise ValidationError("empty condition")
        return pairs

    def _matches(self, condition: str, context: dict) -> bool:
        for path, op, value in self._parse_condition(condition):
            actual = lookup(context, path)
            if isinstance(actual, bool):
                actual = "true" if actual else "false"
            if op == "=":
                if str(actual) != value:
                    return False
                continue
            if op == "!=":
                if str(actual) == value:
                    return False
                continue
            ours, theirs = _as_number(actual), _as_number(value)
            if ours is None or theirs is None:
                # a comparison against something that is not a number is false, not an
                # exception: a condition is edited at runtime and must not take the
                # emitter down when it is wrong
                return False
            if op == ">" and not ours > theirs:
                return False
            if op == ">=" and not ours >= theirs:
                return False
            if op == "<" and not ours < theirs:
                return False
            if op == "<=" and not ours <= theirs:
                return False
        return True

    def _render(self, template: Any, context: dict) -> Any:
        if isinstance(template, str):
            stripped = template.strip()
            if stripped.startswith("${") and stripped.endswith("}") and stripped.count("${") == 1:
                return lookup(context, stripped[2:-1])
            rendered, cursor = "", template
            while "${" in cursor and "}" in cursor:
                head, rest = cursor.split("${", 1)
                path, tail = rest.split("}", 1)
                rendered += head + str(lookup(context, path))
                cursor = tail
            return rendered + cursor
        if isinstance(template, dict):
            return {k: self._render(v, context) for k, v in template.items()}
        if isinstance(template, list):
            return [self._render(v, context) for v in template]
        return template



    # ---- packs ---------------------------------------------------------
    def log(self, team_id: str, since: float, until: float | None = None) -> list[dict]:
        """what actually fired in a window. `fired_count` is a total; this is the
        answer to "what ran last night"."""
        rows = self.store.query(
            "select * from trigger_log where team_id = ? and created_date >= ? and created_date <= ? "
            "order by created_date desc",
            (team_id, since, until if until is not None else now()),
        )
        return [
            {"id": r["id"], "trigger": r["trigger_id"], "event": r["event"],
             "action": r["action"], "job": r["job_id"], "at": r["created_date"]}
            for r in rows
        ]

    def install_pack(self, team_id: str, pack: str = "overnight",
                     dry_run: bool = False) -> dict:
        """adopt a reviewed set of rules in one call.

        the night is a capability until something listens to it. this is that
        something: the rules an operator would have written themselves, written
        once, idempotent by name, and visible afterwards like any other rule.
        """
        if pack not in PACKS:
            raise ValidationError(f"unknown pack '{pack}'", allowed=sorted(PACKS))
        if not self.store.one("select id from teams where id = ?", (team_id,)):
            raise NotFound(f"crew {team_id} not found")
        existing = {r["name"] for r in self.store.query(
            "select name from triggers where team_id = ?", (team_id,))}
        would: list[dict] = []
        created: list[dict] = []
        for rule in PACKS[pack]:
            spec = {
                "name": rule["name"],
                "event": rule["event"],
                "action": rule["action"],
                "payload": {**rule.get("payload", {}), "team_id": "${team_id}"},
                "condition": rule.get("condition", ""),
            }
            would.append(spec)
            if dry_run or spec["name"] in existing:
                continue
            created.append(self.create(
                team_id=team_id,
                name=spec["name"],
                event=spec["event"],
                action=spec["action"],
                payload=spec["payload"],
                condition=spec["condition"],
            ))
        return {"pack": pack, "dry_run": dry_run, "rules": would,
                "created": [c["id"] for c in created],
                "skipped": sorted(existing & {w["name"] for w in would})}


# the overnight pack. two of these exist because a silent failure costs a whole
# night; the rest exist so the morning has something to report.
PACKS: dict[str, list[dict]] = {
    "overnight": [
        {"name": "overnight: brief", "event": "schedule.nightly", "action": "brief",
         "payload": {"team_id": "${team_id}"}},
        {"name": "overnight: roll shifts", "event": "schedule.nightly",
         "action": "shift_roll", "payload": {"team_id": "${team_id}"}},
        {"name": "overnight: fail loudly", "event": "run.failed", "action": "card",
         "payload": {"team_id": "${team_id}", "title": "a run failed: ${error}",
                     "description": "task ${task_id}, run ${run_id}. "
                                    "nobody was watching at the time.",
                     "column": "review"}},
        {"name": "overnight: a2a digest", "event": "schedule.nightly", "action": "brief",
         "payload": {"team_id": "${team_id}", "include_a2a": True}},
        {"name": "overnight: re-dispatch idle", "event": "schedule.nightly",
         "action": "dispatch",
         "payload": {"team_id": "${team_id}", "idle_minutes": 120, "limit": 5}},
    ]
}


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def lookup(context: Any, path: str) -> Any:
    """dotted read into the event context. missing is `None`, never a crash: a
    trigger whose payload path does not exist should enqueue a job with a hole in
    it, not take down the service that emitted the event."""
    current = context
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current
