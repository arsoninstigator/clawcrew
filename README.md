# clawcrew

※ a crew of ai agents that keeps a board, holds shifts, hands work over, and goes to
sleep at night and comes back with a brief.

## what is this

clawcrew is a backend for running a *team* of agents instead of a chat window.

you give it a mission. it gives you a crew: agents with their own names, their own
computer, their own skills, their own memory, and a shared kanban they actually work
from. they talk to each other through agent to agent messages, they hand their cards
over when a shift ends, they escalate to whoever is accountable, and every action
lands in a hash chained audit row attached to the agent that did it.

it is self hosted, byoc and byok. no credits, no per-seat, no hidden orchestration
service in the middle. you bring the machines (or let clawcrew make one on the laptop
you already have) and the model key, and everything else runs in your account.

## the problem

agents today are sessions. you open one, it does something, you close it, and the
work evaporates into a transcript. three things break the moment you try to run more
than one of them:

**the mush.** every agent inherits the same instructions, the same tools, the same
permissions, because they all read from one blob. two weeks later nobody can say why
the writer has production database access. it is not that someone granted it, it is
that nobody ever separated it.

**no shift change.** an agent finishes a task and stops. the next task starts from
zero, with no idea what the last one learned, what is blocked, or which human is
waiting on an answer. a real team solves this with handovers. agents usually do not
have one.

**the human as middleware.** somebody has to read the board, notice the failure,
reassign the card, ask for approval, and remember to check again tomorrow. that is
not a job, it is a tax.

clawcrew is built around the idea that these are structural problems, not prompt
problems. you fix them with rows, keys and boundaries, not with better instructions.

## what a crew actually is

```
crew
├── agents            each with a permanent id, a computer, and nine namespaces
├── channels          rooms with their own board, members, and conversation
├── departments       who is accountable for what, nested, routable
├── tasks             one kanban, sliced by channel, never duplicated
├── workflows         json dags, gated by a human, published to a cron
├── shifts            rotation, so coverage survives a failed beat
└── the night         what ran while you slept, in numbers
```

## the nine namespaces, or: it never becomes mush

every agent carries its own:

- skills
- scope and spec
- memory
- instructions
- knowledge
- permissions (oauth grants, mcps, apis, each with its own expiry)
- tool access (a separate allowlist, not implied by permissions)
- vertical
- spawn policy (can this agent make subagents, and are they ephemeral or permanent)

these are stored as rows keyed by that agent, in nine separate namespaces. there is no
crew wide bucket an agent can write into, so there is nothing to blend. writes replace,
they never deep merge. composition happens once and whole value, and when a composition
loses a value the loser is reported as `shadowed` instead of quietly disappearing.

`GET /agents/{id}/profile` returns the fingerprint of all nine, and
`GET /crews/{id}/isolation` tells you whether two agents have drifted into being the
same agent. subagents start with zero permissions, an off spawn policy, and a lineage
row, so a child can never quietly inherit the parent's reach.

## shifts and handoffs

baristas do not vanish at the end of a shift. they hand over.

a shift plan is a rotation: `slot_index = (now - anchor) // slot`. that is arithmetic,
not a counter, so a duplicated beat cannot skew who is on duty. at the boundary the
outgoing agent leaves a handoff: its open cards, what is blocked, what is waiting on a
human, the last thing said in the room, and one line of next steps. accepting the
handoff transfers the cards in a single transaction and writes an audit row.

async means one owner per slot. sync means the whole rotation working the same board
at the same time, with no acceptance needed.

## the org chart

a department has exactly one accountable coordinator, and departments nest. a card can
be routed to the accountable head's desk, and escalation walks up the reporting line as
a gated agent to agent message, so "ask your manager" is a real operation instead of a
sentence in a prompt.

## agent to agent, and the god view

agents speak A2A on the wire: json rpc, agent cards, message parts, task states. that
is the source of truth, and it is what an outside agent would see.

humans get a different view of the same rows: sentences, in order, with the envelope
kept beside each one. the rendering is never the record. if you delete the pretty
version you lose nothing, because it was derived from the envelope on read.

## the night

a trigger is `event + condition + action + payload`. events come from the system
(card moved, run finished, a2a received, gate decided) and from the clock. actions land
on a real queue.

celery is a core dependency with a beat ticking every fifteen seconds and a nightly
event at 02:00 utc, plus a worker and redis in compose. the queue row is the source of
truth, so a dead worker leaves a requeueable job rather than a lost one.

the morning brief reads the night (18:00 to 06:00 by default, because "overnight" is a
real interval) and reports numbers computed from rows: runs, failures, cards done, rules
fired, and what is waiting on a person. prose is a rendering of those numbers and is
dropped if it disagrees with them. it is delivered as a message in the crew's channel,
so you read it where you already are.

a fresh crew has no rules listening to the night, which is honest but useless, so
`POST /crews/{id}/triggers/pack` installs the overnight pack: brief, failed run to a
card, stale review nudge, and shift rollover. dry run first, by default.

## approvals that do not require logging in

a human gate is a token and a row. the token is signed and carries the decision it
authorises, so one link means "approve" and another means "reject", and neither can be
used twice. opening the link is a read. the button is a post. that is the whole flow:
no account, no dashboard, no daily login.

the parked run resumes when the tap lands, the gate records who and when, and the same
row can be decided over the api by someone who does have an account.

## knowledge, and who may read it

a workspace connector points at your own notion workspace, a folder, a wiki endpoint, or
any http documents source. sync pulls documents into rows, one per external id, updated
in place.

access is granted per agent, by the agent's supervisor, with an expiry, and revoked the
same way. an agent with no grant is refused, and the refusal names the supervisor to ask.
the grant is revocable because it is a row, not an env var.

## maestro

`POST /crews/{id}/ask` takes an outcome in a sentence and returns a reviewed plan: cards
with titles, owners, channels, a definition of done, and dependencies. nothing runs.
approving the ask is one more call and only then do the cards dispatch, in dependency
order.

it is deterministic by default. it reads assignment hints, deadline words, and
dependencies out of the sentence, and it says "no agent available for writing" instead
of inventing one.

## coordination

two primitives, because they are the two things a crew actually needs from a durable
object: **a lease** (this card, this slot, this thread, one agent at a time, with an
expiry so a crashed holder cannot hold the board hostage) and **an append only stream**
(what the room said, in order, readable from a cursor).

sqlite implements both by default, because a crew on one box should not need a cluster.
the interface is four methods (`claim`, `release`, `append`, `read`), so a remote actor
runtime is a second backend rather than a rewrite. `clawcrew coordinator` plans a self
hosted terse durable actors runtime: their helm chart, on your gke, with your bucket, and
`actor_contract()` records which actor holds which primitive. the transport is the one
piece not wired, and the docs say so instead of pretending.

## workflows

a workflow is a json dag of seven node types, validated strictly at save time. agents
can propose workflows, a human approves them, then `publish` binds a cron and the beat
runs it. `human_gate` parks a run until the gate is decided. n8n and dify are called on
your own instances, never copied in.

## computers

every agent gets a computer that keeps its files, its browser profile, and its installed
tooling between tasks. some tasks want the opposite of persistence, so there are two
lifecycles and orchestration never branches on vendor:

- **persistent** - celesto's open source runtime, one microvm per agent
- **disposable** - e2b's open source runtime, one per task, thrown away after

provisioning is native, and it plans before it spends:

```
clawcrew targets                      what this machine can actually do
clawcrew connect aws                  byoc in one step: detect, remember, stop
clawcrew get persistent               print the plan, create nothing
clawcrew get persistent --apply       create it (terraform in your own state)
clawcrew get disposable --target e2b
clawcrew destroy persistent clawcrew-1
```

targets are `local`, `celesto`, `e2b`, `docker`, `ssh`, `k8s`, `aws`, `gcp`, `azure`,
`oracle`, and `superserve` (their sdk, their cloud, opt in, with desktop control and
signed preview links so a human can look at what the agent built). the ssh target is the boring important one: any vps, bare metal box or machine
under your desk that you already pay for. the cloud targets emit terraform you keep, so
the machine is yours to destroy. a plan says what it needs out loud, for example that
e2b's embed runtime needs `/dev/kvm`.

## what is built, and what is not

built and tested: crews, agents, nine namespaces, computers, kanban, channels, a2a with a
human rendering, departments and routing, shifts and handoffs, audit chain, workflows with
gates and publishing, triggers and the overnight pack, the brief, approvals by link,
connectors with supervisor grants, maestro's ask, provisioning plans, and the http api
plus cli.

not built: the remote coordination transport (the local leases and streams ship, the
actor runtime is planned and contracted, not dialled). the visual canvas. pov (the projection, the switcher page, the per agent api)
exists, but the drag and drop builder does not, and neither does a profile editor ui for
those nine namespaces. the calendar inside pov is derived from blocks, schedule targets
and due dates, not synced from a calendar provider. the durable objects layer is designed
and not written (see `docs/oss.md` for what it would sit on).

## quickstart

```
pip install -e .
clawcrew seed                 # a demo crew, a workflow, a few cards
clawcrew demo                 # seed, run, review, end to end, no network
clawcrew serve                # http api on :8000
make test                     # the whole suite, offline
```

the api is documented at `/docs` when the server is up. the cli and the api call the same
service objects, so nothing is cli only or api only.

## where things live

the repo is organised by what you are working on, not by file type: `create/` for making
things, `orgchart/` for who answers for what, `comms/` for talking, `manage/` for operating
agents, `work/` for the work itself. [`directory.md`](directory.md) is the map.

## licence and third party

clawcrew is Apache-2.0. vendored clients keep their own licences in `clawcrew/vendor/*/LICENSE`,
their provenance next to them, and `THIRD_PARTY.md` lists what is copied, what is adapted,
and what is only spoken to over an api. n8n and dify are source available, so only their
export formats are imported here; the clients talk to instances you run.
