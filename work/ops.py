"""the operation vocabulary.

a saga step and a poll check are both "some named thing the system can do", so both
read from one registry instead of each inventing a script language. an op is:

    name         what you write in a workflow or a saga step
    run(args)    do it, return facts
    undo(args, result)   put it back, or say honestly that it cannot be put back

every op here is a call into a service that already exists. nothing new gets a second
implementation just because a saga wants to call it, which is the whole point: the
undo is only trustworthy if the do is the same code path as the api.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from core.errors import NotFound, ValidationError


@dataclass(frozen=True)
class Op:
    name: str
    run: Callable[[dict], dict]
    undo: Callable[[dict, dict], dict] | None = None
    irreversible: bool = False
    note: str = ""


@dataclass(frozen=True)
class Check:
    """a poll check: read one fact from a system, decide nothing."""

    name: str
    run: Callable[[dict], dict]
    note: str = ""


class OpRegistry:
    def __init__(self, ops: list[Op]):
        self._ops = {op.name: op for op in ops}

    def names(self) -> list[str]:
        return sorted(self._ops)

    def get(self, name: str) -> Op:
        op = self._ops.get(name or "")
        if op is None:
            raise ValidationError(f"unknown op '{name}'", ops=self.names())
        return op

    def run(self, name: str, args: dict) -> dict:
        return self.get(name).run(args or {})

    def undo(self, name: str, args: dict, result: dict) -> dict:
        op = self.get(name)
        if op.undo is None:
            raise ValidationError(
                f"op '{name}' has no undo", irreversible=op.irreversible, note=op.note)
        return op.undo(args or {}, result or {})


class CheckRegistry:
    def __init__(self, checks: list[Check], fetcher: Callable[[str, dict], Any] | None = None):
        self._checks = {check.name: check for check in checks}
        self._fetcher = fetcher or _http_json

    def names(self) -> list[str]:
        return sorted(self._checks)

    def get(self, name: str) -> Check:
        check = self._checks.get(name or "")
        if check is None:
            raise ValidationError(f"unknown check '{name}'", checks=self.names())
        return check

    def run(self, name: str, args: dict) -> dict:
        fetch = self._fetcher
        args = dict(args or {})
        if name == "http.json":
            body = fetch(args.get("url", ""), args.get("headers") or {})
            return {"value": dig(body, args.get("path", "")), "body": body}
        return self.get(name).run(args)


def dig(value: Any, path: str) -> Any:
    """dotted path into json, so a check can name the field it cares about."""
    if not path:
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _http_json(url: str, headers: dict) -> Any:
    """one url, one json body. the caller supplies the url, so nothing here decides
    which external system a crew talks to."""
    if not url:
        raise ValidationError("http.json check needs a url")
    request = urllib.request.Request(url, headers={"accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        raise ValidationError(f"check failed with {exc.code}", url=url) from exc
    except (urllib.error.URLError, ValueError) as exc:
        raise ValidationError(f"check failed: {exc}", url=url) from exc


def build_ops(deps: Any) -> OpRegistry:
    """the ops crittertown ships. `deps` carries the services they call."""
    kanban, channels, scheduler = deps.kanban, deps.channels, deps.scheduler

    def task_create(args: dict) -> dict:
        task = kanban.create_task(
            team_id=args["team_id"],
            title=args.get("title") or "untitled",
            description=args.get("description", ""),
            column=args.get("column", "backlog"),
            priority=int(args.get("priority", 0)),
            required_skills=list(args.get("required_skills") or []),
        )
        return {"task_id": task["id"], "column": task["column"], "team_id": task["team_id"]}

    def task_create_undo(args: dict, result: dict) -> dict:
        columns = kanban.columns(result["team_id"])
        target = "cancelled" if "cancelled" in columns else ("backlog" if "backlog" in columns
                                                            else columns[0])
        if result["column"] == target:
            return {"task_id": result["task_id"], "column": target, "changed": False}
        moved = kanban.move(result["task_id"], target, actor="saga", note="undone by saga")
        return {"task_id": moved["id"], "column": moved["column"], "changed": True}

    def task_move(args: dict) -> dict:
        task = kanban.get_task(args["task_id"])
        previous = task["column"]
        moved = kanban.move(args["task_id"], args["column"], actor=args.get("actor", "saga"))
        return {"task_id": moved["id"], "column": moved["column"], "previous": previous}

    def task_move_undo(args: dict, result: dict) -> dict:
        moved = kanban.move(result["task_id"], result["previous"], actor="saga",
                            note="undone by saga")
        return {"task_id": moved["id"], "column": moved["column"]}

    def task_assign(args: dict) -> dict:
        task = kanban.get_task(args["task_id"])
        previous = task.get("assignee_id") or ""
        assigned = kanban.assign(args["task_id"], args.get("agent_id"), actor="saga")
        return {"task_id": assigned["id"], "agent_id": assigned.get("assignee_id") or "",
                "previous": previous}

    def task_assign_undo(args: dict, result: dict) -> dict:
        back = kanban.assign(result["task_id"], result["previous"] or None, actor="saga")
        return {"task_id": back["id"], "agent_id": back.get("assignee_id") or ""}

    def channel_post(args: dict) -> dict:
        message = channels.post(
            args["channel_id"], args.get("text") or args.get("body") or "",
            author_kind=args.get("author_kind", "system"),
            author_id=args.get("author_id", ""),
        )
        return {"message_id": message["id"], "channel_id": args["channel_id"],
                "body": message["body"]}

    def channel_post_undo(args: dict, result: dict) -> dict:
        """a room is append only, so the undo is a retraction, not a deletion. saying
        that out loud is better than a fake delete that quietly rewrites history."""
        message = channels.post(
            result["channel_id"], f"retracted by saga: {result['message_id']}",
            author_kind="system")
        return {"message_id": message["id"], "retracts": result["message_id"]}

    def schedule_create(args: dict) -> dict:
        schedule = scheduler.create(
            team_id=args["team_id"], name=args.get("name") or "scheduled job",
            kind=args.get("kind", "interval"), spec=str(args.get("spec", "300")),
            task_template=args.get("task_template") or {},
        )
        return {"schedule_id": schedule["id"], "team_id": schedule["team_id"]}

    def schedule_create_undo(args: dict, result: dict) -> dict:
        schedule = scheduler.set_enabled(result["schedule_id"], False)
        return {"schedule_id": schedule["id"], "enabled": schedule["enabled"]}

    return OpRegistry([
        Op("task.create", task_create, task_create_undo,
           note="undo cancels the card, it does not erase it: the audit stays"),
        Op("task.move", task_move, task_move_undo),
        Op("task.assign", task_assign, task_assign_undo),
        Op("channel.post", channel_post, channel_post_undo,
           note="a room is append only, so the undo posts a retraction"),
        Op("schedule.create", schedule_create, schedule_create_undo),
    ])


def build_checks(deps: Any, fetcher: Callable[[str, dict], Any] | None = None) -> CheckRegistry:
    kanban, a2a = deps.kanban, deps.a2a

    def task_column(args: dict) -> dict:
        task = kanban.get_task(args["task_id"])
        return {"value": task["column"], "task_id": task["id"]}

    def task_count(args: dict) -> dict:
        tasks = kanban.list_tasks(args["team_id"], column=args.get("column") or "")
        return {"value": len(tasks), "count": len(tasks)}

    def review_pending(args: dict) -> dict:
        queue = a2a.review_queue(args["team_id"])
        return {"value": len(queue), "count": len(queue)}

    def job_status(args: dict) -> dict:
        row = deps.store.one("select status from job_queue where id = ?", (args["job_id"],))
        if not row:
            raise NotFound(f"job {args['job_id']} not found")
        return {"value": row["status"], "status": row["status"]}

    def always(args: dict) -> dict:
        return {"value": bool(args.get("value", True))}

    return CheckRegistry([
        Check("task.column", task_column, "where a card is right now"),
        Check("task.count", task_count, "how many cards sit in a column"),
        Check("review.pending", review_pending, "how many a2a messages wait for a human"),
        Check("job.status", job_status, "the state of one queued job"),
        Check("http.json", always, "one url, one json body, one dotted path out of it"),
    ], fetcher=fetcher)
