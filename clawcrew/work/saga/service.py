"""sagas: a sequence with a way back.

the problem this solves is not "run three things". it is that the third thing fails
after the first two already happened, and now the board says a card exists, a channel
has a message about it, and a schedule is armed for work that will never be done.

so every step declares two operations: the one that does it, and the one that puts it
back. if step n fails, steps n-1 .. 1 are undone **in reverse order** and each undo is
its own row, so the record says what was attempted and what actually came back. the
saga ends in exactly one of:

    done             every step happened
    reverted         a step failed and everything before it was put back
    needs_attention  a step failed and at least one undo failed too

the run is resumable on purpose. a step already `done` is never run twice, and a
half finished compensation finishes where it stopped, because a worker that dies
mid-rollback is the same problem as a worker that dies mid-sequence.
"""

from __future__ import annotations

import re
from typing import Any

from clawcrew.db import Store, dumps, loads, new_id, now
from clawcrew.errors import Conflict, NotFound, ValidationError
from clawcrew.work.ops import OpRegistry

FINAL = {"done", "reverted", "needs_attention"}
STEP_STATUSES = {"pending", "running", "done", "failed", "compensated",
                 "compensation_failed", "no_undo"}

PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_.\-]+)\}")


def render(value: Any, facts: dict) -> Any:
    """`${result.task_id}` and `${steps.make-the-card.task_id}`, nothing more.

    a template language in a rollback path is a place bugs hide, so this resolves a
    dotted path and refuses to invent anything it cannot find.
    """
    if isinstance(value, str):
        def replace(match: re.Match) -> str:
            found: Any = facts
            for part in match.group(1).split("."):
                if isinstance(found, dict) and part in found:
                    found = found[part]
                else:
                    raise ValidationError(f"unknown reference '${{{match.group(1)}}}'")
            return found if isinstance(found, str) else dumps(found).strip('"')
        return PLACEHOLDER.sub(replace, value)
    if isinstance(value, dict):
        return {key: render(item, facts) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, facts) for item in value]
    return value


class SagaService:
    def __init__(self, store: Store, ops: OpRegistry, events_hook=None):
        self.store = store
        self.ops = ops
        self.events_hook = events_hook

    # ---- definition ---------------------------------------------------
    def create(self, team_id: str, name: str, steps: list[dict],
               context: dict | None = None) -> dict:
        if not team_id:
            raise ValidationError("a saga needs a team_id")
        if not isinstance(steps, list) or not steps:
            raise ValidationError("a saga needs at least one step")
        clean: list[dict] = []
        seen: set[str] = set()
        for position, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValidationError(f"step {position} must be an object")
            title = str(step.get("name") or f"step-{position + 1}")
            if title in seen:
                raise ValidationError(f"step name '{title}' is used twice")
            seen.add(title)
            do = step.get("do") or {}
            if not isinstance(do, dict) or not do.get("op"):
                raise ValidationError(f"step '{title}' needs do.op")
            op = self.ops.get(do["op"])
            undo = step.get("undo")
            if undo is None:
                # an op that knows how to undo itself needs no repetition in every
                # step, and an op that cannot is refused here rather than at rollback.
                if op.undo is None and not op.irreversible:
                    raise ValidationError(
                        f"step '{title}' needs an undo: op '{do['op']}' has none",
                        hint="write undo.op, or use an op that declares itself irreversible")
                undo = {"op": do["op"], "args": {}} if op.undo is not None else None
            if undo is not None:
                if not isinstance(undo, dict) or not undo.get("op"):
                    raise ValidationError(f"step '{title}' undo needs an op")
                self.ops.get(undo["op"])
            clean.append({"name": title,
                          "do": {"op": do["op"], "args": do.get("args") or {}},
                          "undo": ({"op": undo["op"], "args": undo.get("args") or {}}
                                   if undo else None)})
        saga_id = new_id("saga")
        stamp = now()
        with self.store.tx() as conn:
            conn.execute(
                """insert into sagas (id, team_id, name, status, steps, context,
                       error, created_date, updated_date) values (?,?,?,?,?,?,'',?,?)""",
                (saga_id, team_id, name or "saga", "draft", dumps(clean),
                 dumps(context or {}), stamp, stamp))
            for position, step in enumerate(clean):
                conn.execute(
                    """insert into saga_steps (id, saga_id, position, name, status,
                           result, error) values (?,?,?,?,'pending','{}','')""",
                    (new_id("sgst"), saga_id, position, step["name"]))
        return self.get(saga_id)

    def get(self, saga_id: str) -> dict:
        row = self.store.one("select * from sagas where id = ?", (saga_id,))
        if not row:
            raise NotFound(f"saga {saga_id} not found")
        return {
            "id": row["id"], "team_id": row["team_id"], "name": row["name"],
            "status": row["status"], "steps": loads(row["steps"], []),
            "context": loads(row["context"], {}), "error": row["error"],
            "created_date": row["created_date"], "updated_date": row["updated_date"],
            "finished_date": row["finished_date"], "progress": self.steps(saga_id),
        }

    def list(self, team_id: str, status: str = "") -> list[dict]:
        if status:
            rows = self.store.query(
                "select id from sagas where team_id = ? and status = ? order by created_date desc",
                (team_id, status))
        else:
            rows = self.store.query(
                "select id from sagas where team_id = ? order by created_date desc", (team_id,))
        return [self.get(row["id"]) for row in rows]

    def steps(self, saga_id: str) -> list[dict]:
        rows = self.store.query(
            "select * from saga_steps where saga_id = ? order by position", (saga_id,))
        return [{"id": r["id"], "position": r["position"], "name": r["name"],
                 "status": r["status"], "result": loads(r["result"], {}),
                 "undo_result": loads(r["undo_result"], {}),
                 "error": r["error"], "started_date": r["started_date"],
                 "finished_date": r["finished_date"]} for r in rows]

    # ---- execution ----------------------------------------------------
    def run(self, saga_id: str) -> dict:
        saga = self.get(saga_id)
        if saga["status"] in FINAL:
            return saga  # already settled, and running it again would not be idempotent
        context = dict(saga["context"])
        context.setdefault("team_id", saga["team_id"])
        context.setdefault("saga_id", saga["id"])
        facts = dict(context)
        facts["steps"] = {}
        for row in saga["progress"]:
            if row["result"]:
                facts["steps"][row["name"]] = row["result"]
        self._set_status(saga_id, "running", "")

        failure = ""
        for step in saga["progress"]:
            if step["status"] == "done":
                continue
            definition = saga["steps"][step["position"]]
            started = now()
            self._set_step(step["id"], "running", started=started)
            try:
                args = render(definition["do"].get("args") or {}, facts)
                result = self.ops.run(definition["do"]["op"], args)
            except Exception as exc:  # noqa: BLE001 - a step failing is a normal outcome
                message = f"{type(exc).__name__}: {exc}"
                self._set_step(step["id"], "failed", error=message, finished=now())
                failure = f"step '{step['name']}' failed: {message}"
                break
            self._set_step(step["id"], "done", result=result, finished=now())
            facts["steps"][step["name"]] = result

        if not failure:
            return self._finish(saga_id, "done", "")

        rolled_back, broken = self._compensate(saga, facts, failure)
        status = "needs_attention" if broken else "reverted"
        message = failure
        if broken:
            message = f"{failure}; {len(broken)} undo(s) failed"
        return self._finish(saga_id, status, message)

    def _compensate(self, saga: dict, facts: dict, reason: str) -> tuple[list[str], list[str]]:
        """reverse order, best effort, and never a silent failure."""
        rolled_back: list[str] = []
        broken: list[str] = []
        completed = [step for step in self.steps(saga["id"])
                     if step["status"] == "done" and step["position"] < self._failed_position(saga)]
        for step in reversed(completed):
            definition = saga["steps"][step["position"]]
            undo = definition.get("undo")
            if not undo:
                self._set_step(step["id"], "no_undo",
                               error="op declares itself irreversible", finished=now())
                continue
            lookup = dict(facts)
            lookup["result"] = step["result"]
            try:
                args = render(undo.get("args") or {}, lookup)
                outcome = self._undo(definition, undo, args, step["result"])
            except Exception as exc:  # noqa: BLE001 - a failed undo is the important case
                self._set_step(step["id"], "compensation_failed",
                               error=f"{type(exc).__name__}: {exc}", finished=now())
                broken.append(step["name"])
                continue
            self._set_step(step["id"], "compensated", undo_result=outcome, finished=now())
            facts["steps"][f"{step['name']}__undo"] = outcome
            rolled_back.append(step["name"])
        return rolled_back, broken

    def _undo(self, definition: dict, undo: dict, args: dict, result: dict) -> dict:
        """two kinds of undo, and the difference matters.

        * an **explicit** undo (`undo: {op, args}`) is another operation to perform: do
          `task.create` and the way back is `task.move` to where it was.
        * an **implicit** undo (the step named no undo, so it came from the op) is the
          op reversing itself, which needs the result it produced.

        calling one as the other is how a rollback quietly does the wrong thing, so the
        distinction is made here and asserted in the tests instead of being inferred.
        """
        if undo["op"] == definition["do"]["op"] and not undo.get("args"):
            return self.ops.undo(undo["op"], args, result)
        return self.ops.run(undo["op"], args)

    def _failed_position(self, saga: dict) -> int:
        for step in self.steps(saga["id"]):
            if step["status"] == "failed":
                return step["position"]
        return len(saga["steps"])

    def _set_status(self, saga_id: str, status: str, error: str) -> None:
        with self.store.tx() as conn:
            conn.execute("update sagas set status = ?, error = ?, updated_date = ? where id = ?",
                         (status, error[:1000], now(), saga_id))

    def _set_step(self, step_id: str, status: str, result: dict | None = None,
                  undo_result: dict | None = None, error: str = "",
                  started: float | None = None, finished: float | None = None) -> None:
        sets = ["status = ?"]
        params: list[Any] = [status]
        if result is not None:
            sets.append("result = ?")
            params.append(dumps(result))
        if undo_result is not None:
            sets.append("undo_result = ?")
            params.append(dumps(undo_result))
        if error or status in {"done", "compensated"}:
            sets.append("error = ?")
            params.append(error[:1000])
        if started is not None:
            sets.append("started_date = ?")
            params.append(started)
        if finished is not None:
            sets.append("finished_date = ?")
            params.append(finished)
        params.append(step_id)
        with self.store.tx() as conn:
            conn.execute(f"update saga_steps set {', '.join(sets)} where id = ?", tuple(params))

    def _finish(self, saga_id: str, status: str, error: str) -> dict:
        stamp = now()
        with self.store.tx() as conn:
            conn.execute(
                "update sagas set status = ?, error = ?, updated_date = ?, finished_date = ? "
                "where id = ?", (status, error[:1000], stamp, stamp, saga_id))
        if self.events_hook is not None:
            saga = self.get(saga_id)
            self.events_hook(f"saga.{status}", {"team_id": saga["team_id"], "saga_id": saga_id,
                                                "error": error})
        return self.get(saga_id)

    # ---- one more useful thing ---------------------------------------
    def retry_failed(self, saga_id: str) -> dict:
        """try the failed step again from where it stopped, compensations already done
        stay done. this is the honest alternative to pretending a rerun is a fresh saga."""
        saga = self.get(saga_id)
        if saga["status"] not in {"reverted", "needs_attention"}:
            raise Conflict("only a reverted or unresolved saga can be retried",
                           status=saga["status"])
        with self.store.tx() as conn:
            conn.execute(
                "update saga_steps set status = 'pending', error = '', result = '{}', "
                "undo_result = '{}' "
                "where saga_id = ? and status in ('failed', 'no_undo', 'compensation_failed')",
                (saga_id,))
            conn.execute("update sagas set status = 'draft', finished_date = null where id = ?",
                         (saga_id,))
        return self.run(saga_id)
