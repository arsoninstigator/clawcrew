"""sqlite storage layer.

one file, wal mode, no migrations framework: the schema is created with
`create table if not exists` and the schema version is pinned in `meta`. this is
deliberate for a v1 backend (a single node, hundreds of agents, thousands of
tasks). the storage surface is small enough that swapping sqlite for postgres
means reimplementing `Store` only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA_VERSION = 2

SCHEMA = """
create table if not exists meta (
    key text primary key,
    value text not null
);

create table if not exists teams (
    id text primary key,
    name text not null,
    slug text not null unique,
    mission text default '',
    review_mode text not null default 'reviewed',
    orchestration text not null default '{}',
    created_date real not null,
    updated_date real not null
);

create table if not exists agents (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    name text not null,
    role text not null default '',
    model text not null default '',
    system_prompt text not null default '',
    skills text not null default '[]',
    tools text not null default '[]',
    runtime text not null default '{}',
    status text not null default 'idle',
    created_date real not null,
    updated_date real not null,
    unique (team_id, name)
);

create table if not exists computers (
    id text primary key,
    agent_id text not null unique references agents(id) on delete cascade,
    provider text not null,
    handle text not null default '{}',
    state text not null default 'pending',
    created_date real not null,
    last_used real
);

create table if not exists tasks (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    parent_id text references tasks(id) on delete set null,
    title text not null,
    description text not null default '',
    column_name text not null default 'backlog',
    priority integer not null default 0,
    assignee_agent_id text references agents(id) on delete set null,
    required_skills text not null default '[]',
    isolation text not null default 'agent',
    payload text not null default '{}',
    result text not null default '{}',
    review_note text not null default '',
    created_date real not null,
    updated_date real not null
);
create index if not exists tasks_team_column on tasks (team_id, column_name, priority desc);

create table if not exists task_events (
    id text primary key,
    task_id text not null references tasks(id) on delete cascade,
    kind text not null,
    actor text not null default 'system',
    data text not null default '{}',
    created_date real not null
);
create index if not exists task_events_task on task_events (task_id, created_date);

create table if not exists runs (
    id text primary key,
    task_id text not null references tasks(id) on delete cascade,
    agent_id text not null references agents(id) on delete cascade,
    provider text not null,
    sandbox_id text default '',
    status text not null default 'queued',
    steps text not null default '[]',
    error text default '',
    started_at real,
    ended_at real,
    created_date real not null
);
create index if not exists runs_task on runs (task_id, created_date);

create table if not exists a2a_messages (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    task_id text references tasks(id) on delete set null,
    from_agent_id text not null references agents(id) on delete cascade,
    to_agent_id text references agents(id) on delete cascade,
    kind text not null default 'note',
    body text not null,
    status text not null default 'pending',
    reviewed_by text default '',
    review_note text default '',
    thread_id text default '',
    created_date real not null,
    reviewed_date real
);
create index if not exists a2a_team_status on a2a_messages (team_id, status, created_date);

create table if not exists context_entries (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    key text not null,
    value text not null,
    author text not null default 'system',
    created_date real not null
);
create index if not exists context_team_key on context_entries (team_id, key);

create table if not exists workflows (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    name text not null,
    version integer not null default 1,
    definition text not null,
    created_date real not null,
    updated_date real not null,
    unique (team_id, name)
);

create table if not exists workflow_runs (
    id text primary key,
    workflow_id text not null references workflows(id) on delete cascade,
    status text not null default 'running',
    state text not null default '{}',
    created_date real not null,
    ended_at real
);

create table if not exists schedules (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    name text not null,
    kind text not null default 'interval',
    spec text not null,
    task_template text not null default '{}',
    enabled integer not null default 1,
    next_run_at real not null,
    last_run_at real,
    created_date real not null
);
create index if not exists schedules_due on schedules (enabled, next_run_at);

-- channels: a crew's slack-like rooms. each one carries its own board, its own
-- members and its own conversation, which is the whole point of the split.
create table if not exists channels (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    name text not null,
    slug text not null,
    purpose text not null default '',
    kind text not null default 'channel',
    created_date real not null,
    updated_date real not null,
    unique (team_id, slug)
);

create table if not exists channel_members (
    channel_id text not null references channels(id) on delete cascade,
    agent_id text not null references agents(id) on delete cascade,
    role text not null default 'member',
    created_date real not null,
    primary key (channel_id, agent_id)
);

create table if not exists channel_messages (
    id text primary key,
    channel_id text not null references channels(id) on delete cascade,
    task_id text references tasks(id) on delete set null,
    author_kind text not null default 'agent',
    author_id text not null default '',
    body text not null,
    created_date real not null
);
create index if not exists channel_messages_channel on channel_messages (channel_id, created_date);

-- departments: the hierarchical layer. a department has one accountable head and
-- reports upward, so routing is a chain instead of a popularity contest.
create table if not exists departments (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    parent_id text references departments(id) on delete set null,
    name text not null,
    slug text not null,
    purpose text not null default '',
    coordinator_agent_id text references agents(id) on delete set null,
    created_date real not null,
    updated_date real not null,
    unique (team_id, slug)
);

create table if not exists department_members (
    department_id text not null references departments(id) on delete cascade,
    agent_id text not null references agents(id) on delete cascade,
    created_date real not null,
    primary key (department_id, agent_id)
);

-- triggers: event -> queued action. the queue is celery's job, the rule is ours.
create table if not exists triggers (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    name text not null,
    event text not null,
    action text not null,
    payload text not null default '{}',
    condition text not null default '',
    enabled integer not null default 1,
    fired_count integer not null default 0,
    last_fired_at real,
    created_date real not null
);
create index if not exists triggers_event on triggers (team_id, event, enabled);

-- an agent's own calendar. per-agent pov needs somewhere to put a block that is
-- not a task and not a schedule.
create table if not exists agent_events (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    agent_id text not null references agents(id) on delete cascade,
    title text not null,
    kind text not null default 'block',
    starts_at real not null,
    ends_at real,
    ref_id text not null default '',
    created_date real not null
);
create index if not exists agent_events_agent on agent_events (agent_id, starts_at);

create table if not exists job_queue (
    id text primary key,
    kind text not null,
    payload text not null default '{}',
    status text not null default 'pending',
    attempts integer not null default 0,
    available_at real not null,
    created_date real not null
);
"""


def new_id(prefix: str) -> str:
    """sortable-ish ids: `<prefix>_<epoch ms base36><random>`."""
    ts = int(time.time() * 1000)
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    body = ""
    n = ts
    while n:
        n, r = divmod(n, 36)
        body = alphabet[r] + body
    return f"{prefix}_{body}{uuid.uuid4().hex[:8]}"


def now() -> float:
    return time.time()


class Store:
    """thin sqlite wrapper. every call opens a short lived connection; wal mode
    plus a busy timeout makes that safe with worker threads."""

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            self._add_columns(c)
            c.execute(
                "insert or replace into meta (key, value) values ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def _add_columns(self, c: sqlite3.Connection) -> None:
        """v2 columns on v1 tables. sqlite has no `add column if not exists`, so
        check the pragma; this stays idempotent for a db created fresh from SCHEMA."""
        wanted = {"tasks": {"channel_id": "text references channels(id) on delete set null"}}
        for table, columns in wanted.items():
            existing = {row["name"] for row in c.execute(f"pragma table_info({table})")}
            for name, decl in columns.items():
                if name not in existing:
                    c.execute(f"alter table {table} add column {name} {decl}")
        c.execute("create index if not exists tasks_channel on tasks (channel_id, column_name)")

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=15.0, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("pragma journal_mode=wal")
        c.execute("pragma foreign_keys=on")
        c.execute("pragma busy_timeout=15000")
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """explicit write transaction, serialized per process."""
        with self._lock:
            c = sqlite3.connect(self.path, timeout=15.0, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("pragma foreign_keys=on")
            c.execute("pragma busy_timeout=15000")
            try:
                c.execute("begin immediate")
                yield c
                c.execute("commit")
            except Exception:
                try:
                    c.execute("rollback")
                except sqlite3.Error:
                    pass
                raise
            finally:
                c.close()

    # ---- helpers -----------------------------------------------------------
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.conn() as c:
            return list(c.execute(sql, params).fetchall())

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None


def dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return {} if default is None else default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return {} if default is None else default
