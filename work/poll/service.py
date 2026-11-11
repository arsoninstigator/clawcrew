"""polls: wait for the world, without writing the waiting loop.

"check the deploy until it is green", "ask the vendor portal until the invoice is
paid" - the code for that is always the same and always slightly wrong: no cap, no
backoff, a sleep that blocks a worker, a retry storm against someone else's api.

so the loop is data. a poll is a check, an interval policy and a stopping rule, and
each attempt is one row plus one queued job, which means:

* a worker never sleeps, it schedules the next attempt (`delay` on the broker)
* intervals grow geometrically up to a cap, so a slow system is not hammered
* it stops on the rule, on a maximum number of attempts, or on a deadline - whichever
  comes first - and it says which one stopped it
* what happens next is a queued action (`on_satisfy` / `on_expire`), not a callback

the check decides nothing. it reads one fact, and the `until` rule decides whether
that fact is the one we were waiting for.
"""

from __future__ import annotations

import random
from typing import Any, Callable

from core.db import Store, dumps, loads, new_id, now
from core.errors import Conflict, NotFound, ValidationError
from work.ops import CheckRegistry, dig

OPEN = "waiting"
FINAL = {"satisfied", "expired", "cancelled", "failed"}
COMPARISONS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "truthy", "falsy", "exists"}
MIN_INTERVAL = 1.0


def evaluate(rule: dict, result: dict) -> bool:
    """one comparison, evaluated against the check's result. no rule, no match."""
    if not rule:
        raise ValidationError("a poll needs an until rule")
    op = str(rule.get("op") or "eq").lower()
    if op not in COMPARISONS:
        raise ValidationError(f"until.op must be one of {sorted(COMPARISONS)}")
    found = dig(result, rule.get("path") or "value")
    if op == "exists":
        return found is not None
    if op == "truthy":
        return bool(found)
    if op == "falsy":
        return not found
    expected = rule.get("value")
    try:
        if op == "eq":
            return found == expected
        if op == "ne":
            return found != expected
        if op == "gt":
            return found is not None and found > expected
        if op == "gte":
            return found is not None and found >= expected
        if op == "lt":
            return found is not None and found < expected
        if op == "lte":
            return found is not None and found <= expected
        if op == "in":
            return found in (expected or [])
        return expected in (found or [])
    except TypeError as exc:  # comparing a string to a number, say
        raise ValidationError(f"cannot compare {found!r} with {expected!r}: {exc}",
                             path=rule.get("path")) from exc


class PollService:
    def __init__(self, store: Store, checks: CheckRegistry, broker, events_hook=None,
                 clock: Callable[[], float] = now, rng: Callable[[], float] = random.random):
        self.store = store
        self.checks = checks
        self.broker = broker
        self.events_hook = events_hook
        self.clock = clock
        self.rng = rng

    # ---- lifecycle ----------------------------------------------------
    def start(
        self,
        team_id: str,
        name: str,
        check: str,
        check_args: dict | None = None,
        until: dict | None = None,
        interval: float = 60,
        max_interval: float = 3600,
        backoff: float = 1.5,
        max_attempts: int = 20,
        deadline: float | None = None,
        on_satisfy: dict | None = None,
        on_expire: dict | None = None,
        jitter: float = 0.0,
        first_delay: float = 0.0,
    ) -> dict:
        if not team_id:
            raise ValidationError("a poll needs a team_id")
        self.checks.get(check)  # refuses an unknown check before anything is stored
        evaluate(until or {}, {"value": True})  # refuses a broken rule up front
        interval = float(interval)
        max_interval = float(max_interval)
        backoff = float(backoff)
        if interval < MIN_INTERVAL:
            raise ValidationError(f"interval must be at least {MIN_INTERVAL:g} seconds")
        if max_interval < interval:
            raise ValidationError("max_interval cannot be smaller than interval")
        if backoff < 1.0:
            raise ValidationError("backoff cannot be below 1.0, that is a retry storm")
        if int(max_attempts) < 1:
            raise ValidationError("max_attempts must be at least 1")
        poll_id = new_id("poll")
        stamp = self.clock()
        next_at = stamp + max(0.0, float(first_delay))
        with self.store.tx() as conn:
            conn.execute(
                """insert into polls (id, team_id, name, check_op, check_args, until, interval,
                       max_interval, backoff, max_attempts, deadline, on_satisfy, on_expire,
                       status, attempts, next_at, last_result, created_date, updated_date)
                   values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,'{}',?,?)""",
                (poll_id, team_id, name or check, check, dumps(check_args or {}),
                 dumps({"until": until or {}, "jitter": float(jitter)}), interval,
                 max_interval, backoff, int(max_attempts), deadline,
                 dumps(on_satisfy or {}), dumps(on_expire or {}), OPEN, next_at, stamp, stamp))
        self.broker.enqueue("poll", {"poll_id": poll_id}, delay=max(0.0, float(first_delay)))
        return self.get(poll_id)

    def get(self, poll_id: str) -> dict:
        row = self.store.one("select * from polls where id = ?", (poll_id,))
        if not row:
            raise NotFound(f"poll {poll_id} not found")
        until = loads(row["until"], {})
        return {
            "id": row["id"], "team_id": row["team_id"], "name": row["name"],
            "check": row["check_op"], "check_args": loads(row["check_args"], {}),
            "until": until.get("until", {}), "jitter": until.get("jitter", 0.0),
            "interval": row["interval"], "max_interval": row["max_interval"],
            "backoff": row["backoff"], "max_attempts": row["max_attempts"],
            "deadline": row["deadline"], "status": row["status"],
            "attempts": row["attempts"], "next_at": row["next_at"],
            "last_result": loads(row["last_result"], {}),
            "on_satisfy": loads(row["on_satisfy"], {}),
            "on_expire": loads(row["on_expire"], {}),
            "created_date": row["created_date"], "finished_date": row["finished_date"],
        }

    def list(self, team_id: str, status: str = "") -> list[dict]:
        if status:
            rows = self.store.query(
                "select id from polls where team_id = ? and status = ? order by created_date desc",
                (team_id, status))
        else:
            rows = self.store.query(
                "select id from polls where team_id = ? order by created_date desc", (team_id,))
        return [self.get(row["id"]) for row in rows]

    def attempts(self, poll_id: str) -> list[dict]:
        rows = self.store.query(
            "select * from poll_attempts where poll_id = ? order by attempt", (poll_id,))
        return [{"attempt": r["attempt"], "status": r["status"],
                 "result": loads(r["result"], {}), "error": r["error"],
                 "created_date": r["created_date"]} for r in rows]

    def cancel(self, poll_id: str, reason: str = "cancelled by a human") -> dict:
        poll = self.get(poll_id)
        if poll["status"] in FINAL:
            return poll
        return self._settle(poll_id, "cancelled", reason)

    # ---- one attempt --------------------------------------------------
    def tick(self, poll_id: str) -> dict:
        """run exactly one check and decide what happens next. no sleeping."""
        poll = self.get(poll_id)
        if poll["status"] != OPEN:
            return {"poll": poll_id, "status": poll["status"], "skipped": True}

        at = self.clock()
        if poll["deadline"] is not None and at >= poll["deadline"]:
            settled = self._settle(poll_id, "expired", "deadline reached")
            return {"poll": poll_id, "status": settled["status"], "reason": "deadline",
                    "attempts": settled["attempts"]}

        attempt = poll["attempts"] + 1
        result: dict = {}
        error = ""
        try:
            result = self.checks.run(poll["check"], poll["check_args"])
            satisfied = evaluate(poll["until"], result)
        except Exception as exc:  # noqa: BLE001 - a failed check is a normal attempt
            satisfied = False
            error = f"{type(exc).__name__}: {exc}"
        self._record_attempt(poll_id, attempt, satisfied, result, error)

        if satisfied:
            settled = self._settle(poll_id, "satisfied", shown=result, attempts=attempt)
            return {"poll": poll_id, "status": settled["status"], "attempts": attempt,
                    "result": result}

        if attempt >= poll["max_attempts"]:
            settled = self._settle(poll_id, "expired",
                                   "max_attempts reached" + (f": {error}" if error else ""),
                                   shown=result, attempts=attempt)
            return {"poll": poll_id, "status": settled["status"], "attempts": attempt,
                    "reason": "max_attempts", "result": result}

        delay = self._next_delay(poll, attempt)
        next_at = at + delay
        with self.store.tx() as conn:
            conn.execute(
                "update polls set attempts = ?, next_at = ?, last_result = ?, updated_date = ? "
                "where id = ?", (attempt, next_at, dumps(result), at, poll_id))
        self.broker.enqueue("poll", {"poll_id": poll_id}, delay=delay)
        return {"poll": poll_id, "status": OPEN, "attempts": attempt, "delay": delay,
                "next_at": next_at, "result": result, "error": error}

    def _next_delay(self, poll: dict, attempt: int) -> float:
        """geometric up to the cap, then flat. jitter is optional and off by default,
        because a predictable schedule is easier to reason about in a test and a log."""
        raw = poll["interval"] * (poll["backoff"] ** max(0, attempt - 1))
        delay = min(raw, poll["max_interval"])
        jitter = float(poll.get("jitter") or 0.0)
        if jitter > 0:
            delay *= 1 + (self.rng() * 2 - 1) * min(jitter, 0.9)
        return max(MIN_INTERVAL, round(delay, 3))

    def _record_attempt(self, poll_id: str, attempt: int, satisfied: bool,
                        result: dict, error: str) -> None:
        with self.store.tx() as conn:
            conn.execute(
                """insert or replace into poll_attempts
                   (id, poll_id, attempt, status, result, error, created_date)
                   values (?,?,?,?,?,?,?)""",
                (new_id("patt"), poll_id, attempt,
                 "satisfied" if satisfied else ("error" if error else "waiting"),
                 dumps(result), error[:1000], self.clock()))
            conn.execute(
                "update polls set attempts = ?, last_result = ?, updated_date = ? where id = ?",
                (attempt, dumps(result), self.clock(), poll_id))

    def _settle(self, poll_id: str, status: str, reason: str = "", shown: dict | None = None,
                attempts: int | None = None) -> dict:
        stamp = self.clock()
        poll = self.get(poll_id)
        with self.store.tx() as conn:
            conn.execute(
                "update polls set status = ?, updated_date = ?, finished_date = ? where id = ?",
                (status, stamp, stamp, poll_id))
        payload = {"poll_id": poll_id, "team_id": poll["team_id"], "reason": reason,
                   "attempts": attempts if attempts is not None else poll["attempts"],
                   "result": shown or poll["last_result"]}
        action = poll["on_satisfy"] if status == "satisfied" else (
            poll["on_expire"] if status == "expired" else {})
        if action and action.get("kind"):
            self.broker.enqueue(str(action["kind"]), dict(action.get("payload") or {}))
        if self.events_hook is not None:
            self.events_hook(f"poll.{status}", payload)
        return self.get(poll_id)


def ensure_open(poll: dict) -> None:
    if poll["status"] != OPEN:
        raise Conflict("this poll is no longer waiting", status=poll["status"])
