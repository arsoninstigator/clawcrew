# clawcrew

![clawcrew](clawcrew/docs/clawcrew.png)

> agent crews that take shifts, hold cards, and sign their own work»

## what is this

clawcrew is a backend for teams of ai agents that actually finish things.

not "chat with an agent". a crew has agents with roles, skills and their own machines. it
has a board, rooms, an org chart, and shifts. work arrives as a card, gets picked up by
whoever is on duty, and shows up again in the morning brief with the numbers attached.

the thing i kept hitting with other agent stacks is that every one of them is a single
loop with a prompt in it. the moment the work is longer than a context window, or there
are two agents, or something needs a human to say yes, you end up being the scheduler.
clawcrew is that scheduler, minus me.

## the shape

```
crew
 ├── agents ── computers (persistent, per agent) ── runs ── cards
 ├── board ── append only event log
 ├── rooms ── conversation + their own board
 ├── org chart ── coordinators, escalation, routing
 ├── shifts ── rotation, on duty, handoffs
 ├── workflows ── dag, agent authored, human approved
 └── schedules ── durable queue, overnight brief
```

## what it does that most agent stacks don't

- **a card is a row, not a message.** every move is validated and appended, so the board
  is an audit log with a current state projection bolted on
- **agents talk through a gate.** in a reviewed crew an a2a message is `pending` until a
  human approves it. an unreviewed claim cannot change another agent's behaviour
- **who is working is arithmetic, not a stored fact.** `(now - anchor) // slot` picks the
  owner, so a duplicated beat cannot skew coverage and a missed beat cannot skip someone
- **approvals are links, not logins.** a gate opens with single use signed tokens and one
  tap decides it. no account, no dashboard visit
- **the night gets counted.** the brief computes numbers from rows, then checks every
  figure in its own prose against them before shipping
- **nothing is trusted to be true.** agents have principals, audit rows are hash chained
  and signed, exportable as a bundle anyone can verify offline
- **an agent never holds a key.** it gets a proxy url and the name of a variable its
  brokered token arrives in, plus the hosts it may reach

## under the hood

python, fastapi, sqlite. one process serves http, one drains the queue, one ticks. swap in
celery when you need more. providers are your own iron first (local microvms, docker, ssh,
k8s, terraform) and somebody else's cloud only when you opt in.

the board is itsaplan, self-hosted: projects, issues, and agents as members with an assignee
slot, so a crew and a person look the same to a project. a crew pulls from it by claiming a
run, and the board decides what woke it up. that is a service the agents call, not a node in
a workflow, and the same rule holds for the rest: n8n and dify are node types because they
are steps inside a flow, langflow is a flow service an agent calls when it wants one.

## where things live

```
agents/ comms/ work/ org/ manage/ create/ sandbox/ integrations/   the code
core/ api/                                                         wiring and the http surface
clawcrew/                                                          glossary, docs, concept, licences, tests
get/                                                               how to run it, one folder per way in
configure.md                                                       every env var, and what it does
```

## status

it runs, it is tested (550 tests, no network, no sleeps), and it is honest about what is
not done: the postgres adapter, env injection into sandboxes, and the npm and mcp entry
points in `get/`. see `clawcrew/docs/remaining.md`.

## licence

apache-2.0. vendored client licences and provenance are in `clawcrew/thirdparty.md`.
