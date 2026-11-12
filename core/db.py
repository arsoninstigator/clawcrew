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

SCHEMA_VERSION = 10

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

-- an agent can work in more than one crew. `team_id` on the agent stays its home
-- crew (where its name is unique); this table is the membership, home included, so
-- "who is in this crew" is one query and nothing is implied by a nullable column.
create table if not exists agent_teams (
    agent_id text not null references agents(id) on delete cascade,
    team_id text not null references teams(id) on delete cascade,
    role text not null default '',
    membership text not null default 'member',
    joined_date real not null,
    primary key (agent_id, team_id)
);
create index if not exists agent_teams_team on agent_teams (team_id);

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

-- triggers: event -> queued action. the queue is schedule's job, the rule is ours.
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

-- appending audit: one hash-chained row per agent action. provenance is not a
-- report you generate later, it is the row you write at the moment of the action.
create table if not exists agent_audit (
    id text primary key,
    team_id text not null,
    agent_id text not null,
    seq integer not null,
    action text not null,
    actor_kind text not null default 'agent',
    actor_id text not null default '',
    target_kind text not null default '',
    target_id text not null default '',
    payload text not null default '{}',
    prev_hash text not null default '',
    hash text not null,
    created_date real not null
);
create index if not exists agent_audit_agent on agent_audit (agent_id, seq);

-- shifts: a rotation is arithmetic, not a cron job. slot_index = (now - anchor) //
-- slot, and who is on duty is rotation[slot_index % len(rotation)]. nothing drifts
-- because nothing is stored except the plan.
create table if not exists shift_plans (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    channel_id text references channels(id) on delete set null,
    name text not null,
    slot_minutes integer not null default 240,
    anchor_at real not null,
    rotation text not null default '[]',
    mode text not null default 'async',
    timezone text not null default 'UTC',
    enabled integer not null default 1,
    created_date real not null,
    updated_date real not null,
    unique (team_id, name)
);

-- a handoff is a shift change with a body: what the outgoing agent knew, which
-- cards are mid-flight, what is blocked, and what the next one should do first.
create table if not exists handoffs (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    plan_id text references shift_plans(id) on delete cascade,
    channel_id text references channels(id) on delete set null,
    slot_index integer not null default 0,
    from_agent_id text references agents(id) on delete set null,
    to_agent_id text not null references agents(id) on delete cascade,
    task_ids text not null default '[]',
    bundle text not null default '{}',
    status text not null default 'open',
    note text not null default '',
    created_date real not null,
    accepted_date real,
    expires_at real
);
create index if not exists handoffs_team on handoffs (team_id, status, created_date);
create unique index if not exists handoffs_slot on handoffs (plan_id, slot_index);

-- workflows can be authored by agents together; approval is a separate verdict so
-- "an agent wrote it" never quietly becomes "it runs every night".
create table if not exists workflow_coauthors (
    workflow_id text not null references workflows(id) on delete cascade,
    agent_id text not null references agents(id) on delete cascade,
    role text not null default 'author',
    verdict text not null default 'pending',
    note text not null default '',
    created_date real not null,
    primary key (workflow_id, agent_id)
);

-- per-agent configuration, one row per (agent, namespace, key). there is no
-- crew-wide bucket here on purpose: isolation is structural, not a convention.
create table if not exists agent_profile (
    id text primary key,
    agent_id text not null references agents(id) on delete cascade,
    namespace text not null,
    key text not null,
    value text not null default '{}',
    version integer not null default 1,
    origin text not null default 'agent',
    created_date real not null,
    updated_date real not null,
    unique (agent_id, namespace, key)
);
create index if not exists agent_profile_ns on agent_profile (agent_id, namespace);

-- who spawned whom. a child never inherits permissions, so this row is lineage and
-- bookkeeping, not an access path.
create table if not exists agent_spawns (
    id text primary key,
    team_id text not null,
    parent_agent_id text not null references agents(id) on delete cascade,
    child_agent_id text not null references agents(id) on delete cascade,
    mode text not null default 'ephemeral',
    task_id text,
    status text not null default 'active',
    created_date real not null,
    retired_date real
);
create index if not exists agent_spawns_parent on agent_spawns (parent_agent_id, status);

create table if not exists job_queue (
    id text primary key,
    kind text not null,
    payload text not null default '{}',
    status text not null default 'pending',
    attempts integer not null default 0,
    available_at real not null,
    created_date real not null
);

create table if not exists gates (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    run_id text not null default '',
    node_id text not null default '',
    title text not null default '',
    prompt text not null default '',
    options text not null default '["approve","reject"]',
    channel_id text,
    status text not null default 'open',
    decided text not null default '',
    decided_by text not null default '',
    decided_date real,
    note text not null default '',
    expires_at real not null,
    created_date real not null
);
create table if not exists gate_tokens (
    nonce text primary key,
    gate_id text not null references gates(id) on delete cascade,
    decision text not null,
    used_at real,
    created_date real not null
);
create table if not exists asks (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    text text not null,
    mode text not null default 'plan',
    interpretation text not null default '{}',
    plan text not null default '[]',
    status text not null default 'planned',
    created_by text not null default 'owner',
    idempotency_key text not null default '',
    created_date real not null
);
create table if not exists briefs (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    date text not null,
    window_from real not null,
    window_to real not null,
    numbers text not null default '{}',
    prose text not null default '',
    created_date real not null,
    unique (team_id, date)
);
create table if not exists connectors (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    kind text not null,
    name text not null,
    config text not null default '{}',
    secret_env text not null default '',
    status text not null default 'connected',
    last_sync real,
    doc_count integer not null default 0,
    created_date real not null,
    unique (team_id, name)
);
create table if not exists knowledge_docs (
    id text primary key,
    team_id text not null references teams(id) on delete cascade,
    connector_id text not null references connectors(id) on delete cascade,
    external_id text not null,
    title text not null default '',
    url text not null default '',
    text text not null default '',
    checksum text not null default '',
    updated_at real not null,
    fetched_at real not null,
    unique (connector_id, external_id)
);
create table if not exists trigger_log (
    id text primary key,
    team_id text not null,
    trigger_id text not null,
    event text not null,
    action text not null,
    job_id text not null default '',
    created_date real not null
);

create index if not exists asks_team on asks (team_id, created_date);
create index if not exists gates_open on gates (team_id, status, expires_at);
create index if not exists knowledge_team on knowledge_docs (team_id, connector_id);
create index if not exists trigger_log_team on trigger_log (team_id, created_date);
create table if not exists credential_broker (
    id text primary key,
    team_id text not null default '',
    agent_id text not null references agents(id) on delete cascade,
    label text not null default '',
    proxy_url text not null default '',
    token_env text not null default '',
    scopes text not null default '[]',
    status text not null default 'active',
    issued_by text not null default 'owner',
    expires_at real,
    revoked_at real,
    created_date real not null,
    unique (agent_id, label)
);
create index if not exists credential_broker_agent on credential_broker (agent_id, status);

-- comms: the signed room log. `comms_events` is the authority; the channel
-- conversation is a view of it, which is why a message carries the event id.
create table if not exists comms_identities (
    agent_id text primary key references agents(id) on delete cascade,
    team_id text not null references teams(id) on delete cascade,
    handle text not null default '',
    pubkey text not null unique,
    seckey text not null default '',
    custody text not null default 'local',
    relays text not null default '[]',
    created_date real not null
);

create table if not exists comms_members (
    room_id text not null references channels(id) on delete cascade,
    member_id text not null,
    member_kind text not null default 'agent',
    pubkey text not null default '',
    role text not null default 'member',
    joined_date real not null,
    primary key (room_id, member_id)
);

create table if not exists comms_events (
    seq integer primary key autoincrement,
    id text not null unique,
    room_id text not null,
    kind integer not null,
    pubkey text not null,
    author_id text not null default '',
    author_kind text not null default 'external',
    content text not null default '',
    tags text not null default '[]',
    created_at integer not null,
    sig text not null default '',
    received_at real not null
);
create index if not exists comms_events_room on comms_events (room_id, seq);
create index if not exists comms_events_created on comms_events (room_id, created_at);

create table if not exists comms_cursors (
    name text primary key,
    room_id text not null default '',
    seq integer not null default 0,
    updated_date real not null
);

create table if not exists comms_bridges (
    room_id text not null references channels(id) on delete cascade,
    transport text not null,
    config text not null default '{}',
    cursor_seq integer not null default 0,
    last_sync real,
    last_error text not null default '',
    primary key (room_id, transport)
);
-- a saga: steps in order, each with an undo, reversed when a later step fails.
create table if not exists sagas (
    id text primary key,
    team_id text not null,
    name text not null default '',
    status text not null default 'draft',
    steps text not null default '[]',
    context text not null default '{}',
    error text not null default '',
    created_date real not null,
    updated_date real not null,
    finished_date real
);
create index if not exists sagas_team on sagas (team_id, status);

create table if not exists saga_steps (
    id text primary key,
    saga_id text not null references sagas(id) on delete cascade,
    position integer not null,
    name text not null,
    status text not null default 'pending',
    result text not null default '{}',
    undo_result text not null default '{}',
    error text not null default '',
    started_date real,
    finished_date real,
    unique (saga_id, position)
);
create index if not exists saga_steps_saga on saga_steps (saga_id, position);

-- a poll: check an external fact on a schedule until a rule says stop.
create table if not exists polls (
    id text primary key,
    team_id text not null,
    name text not null default '',
    check_op text not null,
    check_args text not null default '{}',
    until text not null default '{}',
    interval real not null default 60,
    max_interval real not null default 3600,
    backoff real not null default 1.0,
    max_attempts integer not null default 20,
    deadline real,
    on_satisfy text not null default '',
    on_expire text not null default '',
    status text not null default 'waiting',
    attempts integer not null default 0,
    next_at real,
    last_result text not null default '{}',
    created_date real not null,
    updated_date real not null,
    finished_date real
);
create index if not exists polls_team on polls (team_id, status);

-- one row per browser an agent has been given: which provider, whose task, and
-- whether the profile survives. a browser nobody can find is a login nobody can revoke.
create table if not exists browser_sessions (
    id text primary key,
    team_id text not null default '',
    agent_id text not null,
    task_id text not null default '',
    provider text not null,
    scope text not null default 'task',
    state text not null default 'ready',
    cdp_url text not null default '',
    live_url text not null default '',
    profile_dir text not null default '',
    handle text not null default '{}',
    created_date real not null,
    closed_date real
);
create index if not exists browser_sessions_agent on browser_sessions (agent_id, state);

create table if not exists poll_attempts (
    id text primary key,
    poll_id text not null references polls(id) on delete cascade,
    attempt integer not null,
    status text not null,
    result text not null default '{}',
    error text not null default '',
    created_date real not null,
    unique (poll_id, attempt)
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
        wanted = {
            "tasks": {"channel_id": "text references channels(id) on delete set null"},
            # which side wrote the row: the app, or the signed log behind it
            "channel_messages": {"via": "text not null default 'app'"},
            # immutable public handle: alphanumeric, generated once, never updated
            "agents": {
                "agent_key": "text",
                "scope": "text not null default '{}'",
                # the principal: bech32 of sha256(pubkey). set once, signed, never moved
                "agentid": "text",
                # what the agent is good at, and the lines of work it serves
                "specializations": "text not null default '[]'",
                "verticals": "text not null default '[]'",
            },
            # a spawn is a principal with a parent, and a lane in a fan-out
            "agent_spawns": {
                "subagent_id": "text",
                "parent_agentid": "text",
                "fan_out": "integer not null default 0",
                "nonce": "text",
            },
            # signatures on the provenance chain
            "agent_audit": {
                "signer": "text not null default ''",
                "pubkey": "text not null default ''",
                "sig": "text not null default ''",
            },
            # a workflow is inert until its status says otherwise
            "workflows": {
                "status": "text not null default 'active'",
                "proposed_by_agent_id": "text",
                "approved_by": "text not null default ''",
                "approved_date": "real",
            },
        }
        for table, columns in wanted.items():
            existing = {row["name"] for row in c.execute(f"pragma table_info({table})")}
            for name, decl in columns.items():
                if name not in existing:
                    c.execute(f"alter table {table} add column {name} {decl}")
        c.execute("create index if not exists tasks_channel on tasks (channel_id, column_name)")
        c.execute("create unique index if not exists agents_key on agents (agent_key)")
        c.execute("create unique index if not exists agents_agentid on agents (agentid)")
        c.execute("create unique index if not exists agent_spawns_subagent "
                  "on agent_spawns (subagent_id)")
        self._backfill_memberships(c)

    def _backfill_memberships(self, c: sqlite3.Connection) -> None:
        """every agent that predates multi-crew membership gets its home row, so the
        membership table is the answer to "who is in this crew" from the first boot."""
        c.execute(
            """insert or ignore into agent_teams (agent_id, team_id, role, membership, joined_date)
               select id, team_id, role, 'home', created_date from agents"""
        )

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
