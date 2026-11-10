# directory

the map, and the rule behind it: a folder is a thing you would talk about out loud.

```
clawcrew/
├── api.py                     fastapi app, one route per verb, no business logic
├── app.py                     composition root: services wired once, hooks attached
├── cli.py                     the same services, from a terminal
├── config.py                  settings, all env, no hidden defaults
├── db.py                      sqlite, migrations, json columns, ids, clocks
├── errors.py                  the error ladder and their http codes
├── web.py                     the two served pages (pov switcher, approval gate)
│
├── create/                    making things
│   ├── plans.py               the plan/step shape every target prints before it acts
│   ├── coordination/          leases and streams: sqlite today, actor runtime planned
│   │   └── durable_actors.py  their helm chart as a plan, and the actor contract
│   ├── agents/                the run loop (orchestrator) and the model runtime
│   ├── crew/                  the team itself: agents, context, computers, review mode
│   ├── workflow/              the dag engine, validation, gates, publishing
│   │   └── services/          n8n and dify clients (your instance, their api)
│   ├── sandbox/               provider registry, celesto, e2b, local, http
│   │   └── provision.py       native byoc provisioning: plan, apply, destroy, connect
│   └── intake.py              the front door: an outcome in, a reviewed plan out
│
├── orgchart/                  who answers for what
│   ├── department/            coordinators, nesting, routing, escalation
│   └── authoring/             importing workflows somebody else already wrote
│
├── comms/                     how agents and humans hear each other
│   ├── a2a/                   service, protocol (json rpc, cards, parts), human view
│   └── channels/              rooms: membership, its own board, conversation
│
├── manage/                    operating the crew
│   ├── agent_profiles/        the nine namespaces, fingerprints, consistency
│   ├── provenance/            one hash chained audit row per agent action
│   ├── pov/                   one agent's workspace, as a projection
│   ├── approvals/             gates: signed links, one tap, no login
│   └── knowledge/             workspace adapters and supervisor granted access
│
├── work/                      the work itself
│   ├── tasks/                 the kanban, columns, events, channel slicing
│   ├── shifts/                rotation, on duty, handoff bundles, acceptance
│   ├── schedule/              cron, the job queue, the worker, celery beat
│   ├── overnight/             the brief: numbers from rows, prose checked against them
│   └── triggers/              event + condition + action, and the packs
│
└── vendor/                    third party clients, unmodified, with licences
```

## conventions

**one direction of dependency.** `api.py` and `cli.py` call `app.py`, `app.py` calls the
services in the five verticals, and a vertical may call `db`, `errors`, `config` and
another vertical through a service it was handed, never through an import cycle. if you
need two verticals to talk, wire it in `app.py`, which is the only file that knows the
whole shape.

**no business logic in routes.** a route validates, calls one service method, and returns.
that is why every cli command and every route do the same thing, and why a test can call
the service directly without an http client.

**every action is attributable.** anything that changes state either carries an agent id
and writes an audit row, or is deliberately not audited because no agent was behind it
(the event bridge and the gate opener follow this rule, and say so in a comment).

**no cross tenant reads.** every table that belongs to a crew has `team_id`, every query
filters on it, and the isolation report is what catches the day someone forgets.

**errors are a ladder, not a string.** `errors.py` defines the codes; the api maps them to
statuses in one place; a client can switch on the code without parsing prose.

## tests

`tests/` mirrors the verticals. `base.py` gives a fresh app per test with a temp sqlite
file and no network. anything that would sleep is either injected (a clock) or asserted as
arithmetic (shift rotation), so the whole suite runs offline in well under a minute.

## docs

- `docs/remaining.md` - the spec for what is not built, written before the code
- `docs/oss.md` - the open source landscape: what we adapt, what we only talk to, why
- `SELF_HOSTING.md` - running it for real
- `THIRD_PARTY.md` - what is vendored and under which licence
