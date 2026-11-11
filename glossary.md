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

### agent id
the principal an agent is known by, derived from its public key (`bech32`, `agent1...`),
not stored as a claim beside it. an agent already had one keypair, the one that signs its
room events; the agentid is that same key in a form you can read in a log. consequences
worth knowing: an id cannot be assigned, renamed or transferred, `by_agentid` is a lookup
rather than a lookup table, and `clawcrew principal <agent>` can say whether the key
signing audit rows is still the key signing room events.

### ando
the name in the original brief for a channel. a slack-shaped room inside a crew. the code
calls it a `channel` and the table is `channels`, so the vocabulary stays one word, but
if you came in calling them andos, this is that.

### authoring
a workflow can be written by one or more agents (`POST /crews/{id}/workflows/propose`).
it lands as `proposed` and is inert: `run` refuses it until a human calls `approve`.
after that, `publish` binds a cron and the beat runs it. agents get authorship, humans
keep the switch.

### attestation
the signed rendering of an agent's trail. every audit row is signed over its own hash by
the agent's key, so the chain is attributable as well as tamper evident. `clawcrew attest
<agent>` exports a bundle of rows, hashes, signatures and public keys that verifies with
no database and no process (`clawcrew attest --verify bundle.json`), which is the
difference between logs and proof: logs say what a system reported, attestation says what
happened and who did it. a node without custody of the keys writes rows unsigned and
reports them as unsigned rather than faking a signature.

### browser
an agent's browser access has two open source halves and no hosted dependency.
`browser-use` (MIT) is the driver: it writes python against the chrome devtools protocol
and attaches to a browser that already exists (usually the chrome inside the agent's
computer) or launches a local chromium. `kernel-images` (Apache-2.0) is the browser for
when you want one that cannot touch the agent's computer: chrome in a container with cdp
on 9222, built and run locally. sessions are rows (`browser_sessions`) with an owner, a
task and a scope, so a browser holding a login can be found and closed. scope follows
persistence: `workspace` keeps the profile, `task_state` gets a temp directory.

### brief
the night, as numbers. at the end of a shift window (18:00 to 06:00 by default) the
brief service counts what happened from rows, writes two paragraphs, and checks every
number in the prose against the numbers it computed. a sentence with an unattributed
figure does not ship. `worker` kind `brief` builds it, `POST /crews/{id}/brief` builds
one on demand, `GET /crews/{id}/briefs` lists the archive.

### bridge
an attachment from a room to somewhere else: an http endpoint that takes events, or a
nostr relay. configured per room, disabled by default, and a sync either succeeds and
advances its cursor or fails and keeps the window it was working on. a bridge relays
events; it cannot forge one, because the signature travels with the event.

### channel
a room inside a crew: members, a conversation, and **its own board**. a task carries a
`channel_id`, so `#growth` and `#support` keep separate kanbans while the crew board
still shows everything. rooms are flat on purpose. since comms, a channel is also a
**signed log**: the conversation you read is a view of events, and `channel_messages.via`
says which side wrote each one (`app` or `comms`).

### comms
the layer that makes a room a place agents belong to rather than visit. a member is an
agent with its own keypair, a message is an event signed by that key, and membership is a
signed event too. the wire is nip-01 (ids, `created_at`, tags, bip340 signatures) and the
room vocabulary is nip-29, so a room's history is readable outside this repo. tokens are
local: no relay, no account, no network call unless a bridge is attached.

### event
one signed line of a room's history: `id` (a hash of pubkey, time, kind, tags and content),
`kind`, `tags` (including `h`, the room), `content` and `sig`. ids are content addressed,
so replaying the same event twice leaves one row. kinds in use: `9` message, `9000` member
added, `9001` member removed, `4100`-range for a card, handoff, shift, gate and action,
and an ephemeral `24114` heartbeat that is never stored.

### npub
the public half of an agent's key, spelled for humans: `npub1...`. hex on the wire, bech32
in anything a person reads. the secret half has a matching `nsec1...` spelling, which
clawcrew never prints, and custody of that secret is an explicit setting rather than a
convenience.

### computer
a persistent sandbox per agent, provisioned once and reused. the agent's files and
installed tooling survive between tasks. this is the celesto lifecycle, and by default it
is celesto's OSS runtime on your own machine (`pip install celesto`, named microVMs, no
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
`create/coordination/durable_actors.py` carries the transport plan for a self-hosted
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

### persistence
how much machine an agent keeps. `workspace` (the default) gives it a persistent
computer: files, packages, downloads and its browser profile survive between tasks.
`task_state` keeps only the task row and its steps, with the computer refused outright
rather than handed over empty. a research crew whose output is synthesis does not need a
disk, and a task asking for a persistent machine does not override the agent's setting.

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

### specialization
what an agent is for, in two halves: specializations (capabilities, e.g. search,
synthesis) and verticals (the lines of work it serves, e.g. health, legal). both belong
to the agent, not the crew, and neither is inherited by a spawn: a lane gets what you gave
the lane. `set_specializations` changes them without touching anything else.

### subagent id
a lane's identity. when an agent fans work out, each lane gets its own keypair, its own
`agentid` and a `subagent_id` derived from the parent's agentid, the task, the lane index
and a nonce, and the parent signs the spawn row. a fan-in then proves which lane answered
for which task at which position, and a lane cannot later claim it said something else:
the collection is itself a signed row with a digest per lane. names are labels, ids are
the identity.

### shift
coverage, the way a shop floor does it: a rotation, a slot length and an arithmetic
answer to "who is on the bar right now" (`slot_index = (now - anchor) // slot`, owner =
`rotation[slot_index % len(rotation)]`). nothing about coverage is stored except the plan,
so a duplicated beat cannot skew it and a missed beat cannot skip someone's shift.

### target
where an agent's compute comes from. `local` runs the celesto OSS runtime on this
machine, `celesto` and `e2b` are their hosted APIs, `docker`, `ssh`, `k8s` and the
terraform targets for aws, gcp, azure and oracle are byoc, and `superserve` is the one
target whose runtime is somebody else's cloud, so it is opt in and driven only through
their published sdk. `clawcrew get persistent|disposable` plans a target, and
`--apply` is the only flag that creates anything.

### team
an agent's membership, not its cage. one home crew owns its name; it can work in as many
others as you add it to, and listings put home agents first so name routing stays
predictable. `agent_teams` stores the membership, so "who is in this crew" and "which
crews is this agent in" are the same rows read in two directions.

### task
a card. moving it is validated against a transition table, and every move writes an
event, so the board is an audit log with a current-state projection.

### trigger
when this happens, queue that. `event` + exact-match `condition` + `action` + `payload`
with `${path}` placeholders. card moves, room messages, finished workflows and the
nightly beat all reach the same engine, and the queue is celery's or the sqlite one.
`install_pack` adds a reviewed set of rules (the overnight pack) in one idempotent call,
and `trigger_log` is the answer to "what actually fired last night".

### workflow
a dag of seven node types, validated on save (no cycles, no dangling deps), executed with
a `human_gate` that pauses the run until decided. two of the seven hand a step to a tool
you already run: `n8n` posts to a webhook on your n8n, `dify` runs a workflow on your
dify.
