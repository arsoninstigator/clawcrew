# crittertown

![crittertown](crittertown/docs/crittertown.png)

> agent crews that take shifts, hold cards, and sign their own work

## the name

the whole thing is a town: `critter.town`, `crittertown` in the code. the teams inside it
are still crews, and the slack-shaped place where people and crews talk is `basecamp`. the
town is the frame that holds several crews, one board and a set of machines; a crew is the
unit of work. `crittertown/glossary.md` keeps the words the api uses apart from the ones we
coined.

## what is this

crittertown is a backend for teams of ai agents that actually finish things.

not "chat with an agent". a crew has agents with roles, skills and their own machines. it
has a board, rooms, an org chart, and shifts. work arrives as a card, gets picked up by
whoever is on duty, and shows up again in the morning brief with the numbers attached.

the thing i kept hitting with other agent stacks is that every one of them is a single
loop with a prompt in it. the moment the work is longer than a context window, or there
are two agents, or something needs a human to say yes, you end up being the scheduler.
crittertown is that scheduler, minus me.

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

python, fastapi, sqlite. one process serves http, one drains the queue, one ticks. swap in a
broker when you need more. providers are your own iron first (local microvms, docker, ssh,
k8s, terraform) and somebody else's cloud only when you opt in.

the board is `manage`, self-hosted: projects, issues, and agents as members with an assignee
slot, so a crew and a person look the same to a project. a crew pulls from it by claiming a
run, and the board decides what woke it up.

## the parts

four roots, and the difference between them is who owns the thing:

```
ingredients/   baked in. the platform runs it and hands it to an agent by capability:
               machines/{persistent,ephemeral,hosted}, browser, schedule, durableobjects,
               workflows, projectmanagement/{manage,kanban}, credentials/credential-broker
toolbox/       called. someone else's daemon over its own api, never copied in: the two
               workflow engines, and firecrawl as the worked example of a tool an agent
               calls (scrape, crawl, map, through your own key)
workbench/     later: where an agent composes its own steps out of toolbox entries
infrastructure/ later: how the platform itself is deployed
```

an ingredient is named for what it does, not for the project it came from. `persistent` and
`ephemeral` are machines, `browser` is a page, `schedule` is the queue, `workflows` is a
flow, `manage` is the board, the credential broker is how an agent borrows a key without
holding one. `crittertown/thirdparty.md` is the one file that names upstreams, and it is also
the argued case for every licence here.

**an agent vm gets none of it.** the platform carries ingredients because the platform is
what runs them. an agent gets one computer and one transport, and asks the platform for the
rest. `docker build --target agent-vm` is the proof: there is no `ingredients/` in that
image, and a test asserts it.

## where things live

```
agents/ comms/ work/ org/ manage/ create/ sandbox/   the platform's own code
ingredients/                                        baked in, renamed for what it does
toolbox/                                            called over an api, never copied in
core/ api/                                          wiring and the http surface
crittertown/                                           glossary, docs, concept, credits, tests
theidea/                                            how it was assembled, and out of what
get/                                                how to run it, one folder per way in
configure.md                                        every env var, and what it does
```

## status

it runs, it is tested (568 tests, no network, no sleeps), and it is honest about what is
not done: the postgres adapter, env injection into sandboxes, and the npm and mcp entry
points in `get/`. see `crittertown/docs/remaining.md`.

## licence

apache-2.0. vendored client licences and provenance are in `crittertown/thirdparty.md`.
