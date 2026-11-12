# glossary

every word the readme uses, defined once. if a term here has a table next to it, that
table is the shape the code stores, and the definition is what the tests hold it to.

### a2a
every agent-to-agent message is a row with a sender, a recipient, a task and a thread. in
a `reviewed` crew the message is `pending` and `inbox()` does not return it until someone
approves it. that is the whole point: an unreviewed claim cannot change another agent's
behaviour. on the wire it is spec-shaped a2a json over json-rpc (`/a2a/rpc`, agent cards
at `/agents/{id}/card`).

### agent
a specialist: a role, skills, a system prompt, a runtime (persistent computer or
ephemeral sandbox) and tools. three seed agents are enough to feel like a team: a
researcher who cites, a writer who only writes from crew research, and an operator who
ships.

### ando
the name in the original brief for a channel. a slack-shaped room inside a crew. the code
calls it a `channel` and the table is `channels`, so the vocabulary stays one word, but
if you came in calling them andos, this is that.

### authoring
a workflow can be written by one or more agents (`POST /crews/{id}/workflows/propose`).
it lands as `proposed` and is inert: `run` refuses it until a human calls `approve`.
after that, `publish` binds a cron and the beat runs it. agents get authorship, humans
keep the switch.

### brief
the night, as numbers. at the end of a shift window (18:00 to 06:00 by default) the
brief service counts what happened from rows, writes two paragraphs, and checks every
number in the prose against the numbers it computed. a sentence with an unattributed
figure does not ship. `worker` kind `brief` builds it, `POST /crews/{id}/brief` builds
one on demand, `GET /crews/{id}/briefs` lists the archive.

### channel
a room inside a crew: members, a conversation, and **its own board**. a task carries a
`channel_id`, so `#growth` and `#support` keep separate kanbans while the crew board
still shows everything. rooms are flat on purpose.

### computer
a persistent sandbox per agent, provisioned once and reused. the agent's files and
installed tooling survive between tasks. this is the persistent lifecycle, and by default it
is persistent's OSS runtime on your own machine (`pip install persistent`, named microVMs, no
account). see `SELF_HOSTING.md`.

### connector
workspace knowledge an agent is allowed to read: a folder, a git repo, an http endpoint.
a connector is a row with a kind, a config and a secret **name**, never a secret value. a
supervisor grants an agent a connector for a bounded number of hours and within its own
department, and every read lands in the agent's audit chain. `GET
/agents/{id}/knowledge` is the search; `/connectors/*` is the lifecycle.

### coordination
leases and streams, so two agents cannot both claim the same card and an agent can read
what happened before it woke up. the contract is `claim`, `release`, `append`, `read`;
the tested backend today is the same sqlite file as everything else, and
`create/coordination/durableobjects.py` carries the transport plan for a self-hosted
durable actors runtime.

### crew
a named group with a mission, a review mode (`reviewed` or `open`), an orchestration
policy, its own kanban columns and its own context. table: `teams`.

### department
the hierarchy, kept explicit: one coordinator, members reporting to it, departments that
nest. routing a card to a department puts it on the coordinator's desk, and escalation
walks up the reporting line as a gated a2a message. hierarchy for accountability,
channels for conversation.

### front door
an outcome in, a reviewed plan out. `POST /crews/{id}/ask` takes a sentence, splits it
into the deliverables it can actually see, places each in a channel and a department,
picks the agent with the right skills and records the whole thing as a plan. plan mode
writes nothing to the board you did not ask for. `approve` is the one-word second step.
it is deterministic on purpose: no model in the path, and the clauses it could not place
are visible instead of smoothed over.

### gate
an approval that needs a link, not a login. a `human_gate` node, or a run that failed,
opens a gate with signed single-use tokens; the paper trail is the row, the question
travels to the crew channel, and one tap on the link decides it. links expire, are bound
to one decision, and are refused after use. tables: `gates`, `gate_tokens`.

### handoff
the shift change, with a body. at every boundary the incoming agent gets a bundle: open
cards (including the ones the outgoing agent was holding), what is blocked, what is
waiting on a human, the room's last words and a next-steps line. accepting it transfers
the cards and writes an audit row. `mode: sync` means the whole rotation works the slot
together, so nobody has to accept; `mode: async` is one owner per slot with a real
handoff.

### profile
every agent carries its own namespaces: `skills`, `scope`, `memory`, `instructions`,
`knowledge`, `perms`, `tools`, `vertical`, `spawn`, `connectors`. all of it is rows keyed
by agent, so there is no crew-wide bucket an agent can write into and isolation is
structural instead of a convention. writes replace, they never deep merge, and
composition happens once, whole-value, with the losing default reported as `shadowed`.
`GET /agents/{id}/profile` returns the config plus a fingerprint and the problems it can
prove; `GET /crews/{id}/isolation` says whether two agents have become the same agent.

### provenance
every agent has one permanent `agent_key` (alphanumeric, generated once, refused on
update) plus a `scope`. every action it takes appends a row to its own hash chain, and
`verify` recomputes the chain, so an edited history is detectable instead of deniable.

### pov
the workspace as one agent sees it: their board, their rooms, their calendar (derived
from explicit blocks, schedule targets and card due dates), their inbox, their computer,
the context they share, and only what needs attention. switching pov is a projection,
never a login, which is why it is cheap and honest.

### run
one dispatch of one task by one agent: provider, sandbox id, per-turn steps, exit codes,
stdout. runs are rows, not logs in a file, so you can query them.

### schedule
interval or five-field cron, durable, feeding the same job queue the api uses. `tick()`
is idempotent; the queue claim is a single serialized update so two workers never take
the same job.

### shift
coverage, the way a shop floor does it: a rotation, a slot length and an arithmetic
answer to "who is on the bar right now" (`slot_index = (now - anchor) // slot`, owner =
`rotation[slot_index % len(rotation)]`). nothing about coverage is stored except the plan,
so a duplicated beat cannot skew it and a missed beat cannot skip someone's shift.

### target
where an agent's compute comes from. `local` runs the persistent OSS runtime on this
machine, `persistent` and `ephemeral` are their hosted APIs, `docker`, `ssh`, `k8s` and the
terraform targets for aws, gcp, azure and oracle are byoc, and `hosted` is the one
target whose runtime is somebody else's cloud, so it is opt in and driven only through
their published sdk. `clawcrew get persistent|disposable` plans a target, and
`--apply` is the only flag that creates anything.

### task
a card. moving it is validated against a transition table, and every move writes an
event, so the board is an audit log with a current-state projection.

### trigger
when this happens, queue that. `event` + exact-match `condition` + `action` + `payload`
with `${path}` placeholders. card moves, room messages, finished workflows and the
nightly beat all reach the same engine, and the queue is schedule's or the sqlite one.
`install_pack` adds a reviewed set of rules (the overnight pack) in one idempotent call,
and `trigger_log` is the answer to "what actually fired last night".

### workflow
a dag of seven node types, validated on save (no cycles, no dangling deps), executed with
a `human_gate` that pauses the run until decided. two of the seven hand a step to a tool
you already run: `n8n` posts to a webhook on your n8n, `dify` runs a workflow on your
dify.

## board

the project management a crew shares, run on manage. projects hold issues, and an agent is
a member with a role and an assignee slot, so assigning an issue to an agent is the same
gesture as assigning it to a person. clawcrew holds an api key per agent and pulls.

## run

what the board hands an agent: the issue, the prompt, the system prompt, and a run id. the
claim is atomic, so a run is either yours or a 409 and you take a different issue. while it
works the agent heartbeats and posts events, and it posts a result even when the turn raised,
because a run left open blocks the issue for everyone else.

## node or service

a node is a step inside a workflow: n8n and dify are nodes. a service is something an agent
calls itself, whenever it decides to: the browser, the sandbox, workflows for a flow, and the
board. the difference is who decides the order. a workflow decides for a node; the agent
decides for a service.

## the four roots

the tree has four roots for things that came from outside, and the difference between them
is who owns the thing, not how big it is.

- **an ingredient** is baked in: the platform runs it, its state is the platform's, and it
  is named for what it does (`persistent`, `ephemeral`, `hosted`, `browser`, `schedule`,
  `durableobjects`, `workflows`, `manage`, `kanban`, `credential broker`). an agent does not
  carry ingredients; it gets one computer and one transport and asks the platform for the
  rest, which is what `docker build --target agent-vm` proves.
- **a toolbox entry** is called: your daemon, your key, over its own api, never copied in.
  the licence is usually why (an agpl tool stays at arm's length), and the shape each one
  has is `configured()`, a `status()` that says what is missing, one method per call that
  returns a plain dict, and a `ProviderFailure` on anything that is not 2xx. `firecrawl` is
  the worked example.
- **workbench** is where an agent composes its own steps out of toolbox entries. not built.
- **infrastructure** is how the platform itself is deployed. not built.

nothing under those four roots is imported by an agent vm, and the boundary test reads the
dockerfile to keep it that way. the upstream project behind an ingredient is named in
`clawcrew/thirdparty.md` and in that ingredient's `PROVENANCE.md`, nowhere else.
