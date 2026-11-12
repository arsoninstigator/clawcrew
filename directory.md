# where things live

the repository has four roots for things that come from outside, and one for the platform's
own code. what separates them is who owns the thing, not what it does.

## the platform

```
agents/        crews, agents, roles, skills, orchestration, importers, adapter registry
comms/         rooms and the signed event log, channels, a2a, protocol, transports, gateway
work/          cards, shifts, triggers, jobs, workflows, ops, saga, poll, overnight
org/           org chart, routing, escalation, verticals
manage/        approvals (gates), knowledge (connectors), audit, attest, approvals ui
create/        intake and coordination: the front door that turns an ask into a crew
sandbox/       the machine abstraction the platform uses: base, transport, http, local,
               provision (terraform specs), and the provider registry
core/          one object graph (app.py), settings, db, errors, services, cli
api/           http routes
```

## ingredients (baked in)

the platform runs these. they are named for what they do; the upstream project is named
only in `clawcrew/thirdparty.md`.

```
ingredients/machines/persistent/      per agent computers that outlive a run
ingredients/machines/ephemeral/       a computer that dies with the task
ingredients/machines/hosted/          the same, in someone else's cloud, opt in
ingredients/browser/                  a page: local driver, container browser, or none
ingredients/schedule/                 schedules, the job queue, the worker, the broker
ingredients/durableobjects/           durable coordination contracts
ingredients/workflows/                a flow, by id, that an agent calls
ingredients/projectmanagement/manage/ the shared board: projects, issues, runs
ingredients/projectmanagement/kanban/ the board a crew keeps for itself
ingredients/credentials/credential_broker/  grants, proxy url, token env name, host scopes
```

## toolbox (called)

```
toolbox/workflows/    the two workflow engines you run yourself, and their clients
toolbox/firecrawl/    the worked example of a tool an agent calls
```

## later

```
workbench/            where an agent composes its own steps out of toolbox entries
infrastructure/       how the platform itself is deployed
```

## the reading material

```
clawcrew/glossary.md      every word this repo uses, defined
clawcrew/concept/         the long form: how a crew is shaped and why
clawcrew/docs/            backend notes, oss landscape, decisions, what is left
clawcrew/selfhost.md      running it on your own metal
clawcrew/thirdparty.md    the credits file: upstreams, licences, what was taken
clawcrew/tests/           the suite
get/                      one folder per way in: https, api, sdk, npm, mcp
configure.md              every env var
```

## the rule that shapes it

an ingredient exists for the platform. an agent vm is built from the platform's agent side
only (`agents/`, `comms/`, the sandbox transport) and gets a computer at runtime. so an
agent can use project management without carrying a project management daemon, and a new
ingredient never adds weight to every agent on the box.
