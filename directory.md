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
├── crews.py                   the team: agents, context, computers, review mode
├── kanban.py                  the board, its columns, its event log
├── a2a.py                     agent to agent messages, with the review gate
├── a2a_protocol.py            the published json-rpc shape, validated on the way out
├── a2a_human.py               a transcript rendered as sentences
├── channels.py                rooms: membership, its own board, conversation
│
├── comms/                     rooms as a signed log, agents as their members
│   ├── events.py              the nip-01 envelope, kinds, filters, canonical ids
│   ├── identity.py            one keypair per agent, minted once
│   ├── secp256k1.py           bip340 schnorr, in tree, so a signature is a real one
│   ├── bech32.py              npub / nsec spellings
│   ├── service.py             the append-only log, roster, cursors, bridges, sync
│   ├── transports.py          http contract, nostr relay frames, and their refusals
│   └── gateway.py             channel conversation <-> signed log, without a loop
├── departments.py             coordinators, nesting, routing, escalation
├── pov.py                     one agent's workspace, as a projection
├── profile.py                 per-agent namespaces, fingerprints, isolation report
├── audit.py                   one hash chained row per agent action
├── shifts.py                  rotation, on duty, handoff bundles, acceptance
├── triggers.py                event + condition + action, and the packs
├── workflows.py               the dag engine, validation, gates, publishing
├── scheduler.py               cron, the job queue, the worker, celery beat
├── jobs.py                    the celery app, one task
├── orchestrator.py            the run loop: provider, turns, tools, review
├── runtime.py                 the model behind a run, deterministic or an http llm
├── importers.py               workflows somebody else already wrote (n8n, dify)
├── services.py                the self-hosted tools, over their own apis
│
├── sandbox/                   provider registry and the providers themselves
│   ├── base.py                the interface every provider satisfies
│   ├── celesto.py celesto_local.py    hosted microVMs, and the same runtime locally
│   ├── e2b.py e2b_create.py   ephemeral sandboxes
│   ├── superserve.py          opt in, their runtime, their sdk
│   ├── docker.py ssh.py k8s.py        your own iron
│   ├── provision.py           native byoc provisioning: plan, apply, destroy, connect
│   └── transport.py           the one http client every provider shares
│
├── create/                    making things
│   ├── plans.py               the plan/step shape every action prints before it acts
│   ├── intake.py              the front door: an outcome in, a reviewed plan out
│   ├── coordination/          leases and streams, and the durable actors transport plan
│   └── sandbox/ is gone: providers live in clawcrew/sandbox
│
├── manage/                    operating the crew
│   ├── approvals/             gates: signed links, one tap, no login
│   ├── knowledge/             workspace adapters and supervisor granted access
│   └── agent_vault/           brokered credentials: a grant, never a key
│
├── work/                      the work itself
│   └── overnight/             the brief: numbers from rows, prose checked against them
│
└── vendor/                    third party clients, unmodified, with licences
```

## conventions

**one direction of dependency.** `api.py` and `cli.py` call `app.py`, `app.py` calls the
services, and a service may call `db`, `errors`, `config` and another service it was
handed, never through an import cycle. if you need two services to talk, wire it in
`app.py`, which is the only file that knows the whole shape.

**no business logic in routes.** a route validates, calls one service method, and returns.

**one service, one file.** the services that grew their own folder (`create/`, `manage/`,
`work/`) are the ones with more than one concern inside them: coordination has a contract
and two backends, agent_vault has grants and egress policy, overnight has a brief and its
digest. a service that is one idea stays one module at the top.

**no secrets in rows.** a connector stores the *name* of the variable a secret arrives in,
and a vault grant stores the name of the token variable plus the proxy url. a secret in a
row is a secret in a backup.
