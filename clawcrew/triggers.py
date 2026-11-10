"""triggers: when this happens, queue that.

a trigger is a row, not a plugin: `event` (what happened), `condition` (an exact
match on the event context), `action` (a queued job kind) and `payload` (the job's
payload, with `${path}` placeholders filled from the context).

that shape is on purpose. an expression language would be a second product to
maintain, and the honest v1 is: exact matches only, one action per rule, and the
queue does the rest. overnight work and "when a card lands in review, dispatch the
next step" are both just rows here.

the queue is celery's job (or sqlite's when celery is not configured). this module
never executes work itself, so a rule that fires twice at 3am is visible in the job
table instead of hiding in a thread.
"""

from __future__ import annotations

from typing import Any

from .db import Store, dumps, loads, new_id, now
from .errors import NotFound, ValidationError

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
ACTIONS = ("task", "dispatch", "workflow", "schedule")


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
            payload = self._render(trigger["payload"], context)
            job_id = None
            if self.broker is not None:
                job_id = self.broker.enqueue(trigger["action"], payload)
            with self.store.tx() as c:
                c.execute(
                    "update triggers set fired_count = fired_count + 1, last_fired_at = ? where id = ?",
                    (now(), trigger["id"]),
                )
            fired.append(
                {"trigger": trigger["id"], "event": event, "action": trigger["action"],
                 "job": job_id, "payload": payload}
            )
        return fired

    # ---- condition + template ------------------------------------------
    def _parse_condition(self, condition: str) -> list[tuple[str, str]]:
        pairs = []
        for part in str(condition).split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise ValidationError(f"condition '{part}' must look like path=value")
            path, value = part.split("=", 1)
            pairs.append((path.strip(), value.strip()))
        if not pairs:
            raise ValidationError("empty condition")
        return pairs

    def _matches(self, condition: str, context: dict) -> bool:
        for path, value in self._parse_condition(condition):
            actual = lookup(context, path)
            if isinstance(actual, bool):
                actual = "true" if actual else "false"
            if str(actual) != value:
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
