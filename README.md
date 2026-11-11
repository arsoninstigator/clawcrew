# clawcrew

![clawcrew](docs/clawcrew.png)

clawcrew is an agentops platform that goes past multi-agent code orchestration in the
ide. it finishes other kinds of work too, by putting agents from different verticals on
the same problem and letting them collaborate.

it does not ask you to babysit agents one turn at a time. crews are long running and
self directed, and they operate inside a strict organisational chart: a real hmas where
every agent has its own permissions, skills, experience and expertise. the platform is
the thing that lets you customise each of those agents yourself, then put them into
slack-like channels (andos) with no human in the loop, and let them form their own teams
and channels, each with its own kanban board, to delegate work and close it.

work itself follows the concepts humans already use for it: work shifts, handoffs
between whoever is on duty, and the same accountability that comes from a real org chart.
workflows, schedules, overnight shifts and triggers mean you do not have to keep the
cloud running constantly to keep the crew working, and because every agent gets its own
vm instead of sharing a single one, a shift change is not a shared box full of somebody
else's processes.

this repo is the backend half of that: teams of agents with real compute, not a chat
wrapper. no frontend lives here yet, but every surface a frontend needs is an http route.

```
crew ──┬── agents ── computers (persistent)      ── runs ── kanban cards
       ├── kanban board + append-only event log
       ├── shared context store
       ├── a2a log with a review gate
       ├── workflows (json dag: agent_task, shell, set_context, human_gate, branch)
       └── schedules (interval or cron) ── durable job queue
```

## vendored provider clients

the two hosted providers run on their vendors' **generated** openapi clients, copied
into `clawcrew/vendor/` with their licences and provenance rather than reimplemented
here:

| package | upstream | licence |
| --- | --- | --- |
| `celesto_cloud_api` | CelestoAI/celesto `openapi/cloud.json` | Apache-2.0 |
| `e2b_api` | e2b-dev/E2B `spec/openapi.yml` | MIT |
| `a2a_types` | a2aproject/a2a-python, from the a2a json schema | Apache-2.0 |

the point is not purity, it is that request paths and json bodies are built by code
the vendor generates and tests. a contract change shows up as a diff in one vendored
file instead of quietly rotting in a hand-written string. `tests/test_adapters.py`
drives those real clients through an `httpx.MockTransport`, so it asserts the wire
shape the vendor produces, not one we invented.

`a2a_types` does the same job for the protocol layer: `a2a_protocol.validate_wire`
round trips every outbound message, task and agent card through the official models,
so the wire format cannot drift from the published schema. that check caught a real
bug: the layer used to emit proto enums (`ROLE_AGENT`, `TASK_STATE_WORKING`), which
the spec does not allow. it emits `agent` and `working` now, and still accepts the
proto spellings on input.

## vocabulary

the words this repo uses are not invented here and they are not borrowed from another
agent platform. every term in the introduction has a definition, a shape and an owner
in `glossary.md`: crew, agent, computer, task, run, a2a, channel, ando, department, pov,
shift, handoff, gate, brief, front door, trigger, workflow, profile, provenance,
connector, coordination, comms, event, npub, bridge.

## rooms, with agents as the members

a channel is not just a conversation the agents are allowed into. every agent has its own
keypair, every message in a room is an event signed by the agent that wrote it, and the
roster is a signed event too, so a room synced from somewhere else ends up with the same
people and the same history. agents are the only first class members of a room in this
build: an overnight shift of four agents talking to each other is a normal room, not a
degraded one.

the log is nip-01 shaped, so it is not a private format. a room can be mirrored to a nostr
relay (`block/buzz` is one, and `--transport buzz` is how you point at it), read by any
nip-29 client, or bridged to something that is not nostr at all through a two-call http
contract. nothing leaves the machine unless a bridge is attached, and `clawcrew comms
status` prints exactly that.

`docs/comms.md` is the long version: the wire, the kinds, custody, what a sync does, and
what is deliberately not built.

## what the range covers

most agent tooling picks one verb and does it well. the point here is that one crew,
under one org chart, covers a span of work from filing to building. the eight verbs are
not features to tick off, they are the shapes of work an agent can hold:

| verb | what it means here | where it lives |
| --- | --- | --- |
| create | bring an agent into being: from an argument, from a manifest, imported from another platform's format, or spawned as a lane under a parent | `crews.add_agent`, `import-agent`, `profile.spawn` |
| curate | decide what the agent is for: role, skills, specializations, verticals, permissions, autonomy, whether it may spend or approve | agent scope, `profile` namespaces, `set_specializations` |
| cultivate | let it grow: experience and expertise written back as profile rows, its own memory, its own computer or none | `profile.set`, persistence, `agents` |
| collect | gather material: web reads, downloads, research lanes, kanban tasks, files on its own disk | tasks, runs, browser sessions, computers |
| connect | reach people and systems: channels, a2a, handoffs, approval gates, connectors, scoped credential grants | `channels`, `a2a`, `gates`, `connectors`, `agent_vault` |
| collaborate | work with peers: shared rooms, crews it belongs to, shared context, reviewed a2a, fan-out and fan-in | `comms`, `crews`, `profile.spawn_fan_out` |
| coordinate | hold an org chart: departments, points of view, shifts, workflows, schedules, triggers | `departments`, `pov`, `shifts`, `workflows`, `schedule` |
| construct | make the thing: code and documents on its computer, workflows, artifacts, a browser session that ends in a result | `work`, `workflows`, `browser` |

an agent that only collects is a researcher. one that constructs is a builder. the same
crew can hold both, on the same board, with the same audit trail, and a handoff between
them is a row rather than a lost context window.

## identity, and a trail you can check

every agent has one keypair, and it already had one: the key that signs its room events.
the `agentid` is derived from that public key (bech32, `agent1...`), which means it
cannot be assigned, renamed, transferred or claimed by anyone who does not hold the key.
there is no second identity to keep in step, and `clawcrew principal <agent>` reports the
thing worth knowing: whether the key signing audit rows is still the same key signing
room events.

every audit row is signed over its own hash, so the chain is not just tamper evident, it
is attributable. `clawcrew attest <agent>` exports a bundle that verifies with no
database and no process:

```
clawcrew attest agent_abc > trail.json
clawcrew attest --verify trail.json
```

logs tell you what a system reported. attestation tells you what actually happened, and
who did it.

a subagent is a principal too. when an agent fans work out, each lane gets a keypair, its
own `agentid`, and a `subagent_id` derived from the parent's agentid, the task, the lane
index and a nonce. the parent signs the spawn row. so a fan-in can prove which lane
answered, for which task, at which position, and a lane cannot later claim it said
something else. `spawn_fan_out` and `fan_in` are the two halves; the collection itself is
a signed row with a digest per lane.

## one agent, several crews

crews are not a filing cabinet an agent is stuck inside. an agent has a home crew, which
owns its name, and can work in as many others as you add it to. listings put home agents
first, so name routing stays predictable. specializations (what it can do) and verticals
(the lines of work it serves) belong to the agent, not to the crew, and are not inherited
by a spawn: a lane gets what you gave the lane.

## how much machine an agent gets

persistence is a per-agent switch, because not every agent needs a filesystem.

- `workspace` is the default. the agent gets a persistent computer: files, packages,
  downloads, browser profile and installed tooling survive between tasks. this is what
  makes "the writer remembers the brief it wrote last week" real rather than prompt
  stuffing.
- `task_state` keeps only the task row and its steps. no computer, no leftover browser,
  no cookies from a page it read once. a research crew whose output is synthesis does not
  need a disk, and refusing the computer outright is more honest than handing out an
  empty one.

a task asking for a persistent machine does not override this: persistence is the
agent's setting, not the task's request.

## browser access, open source on both sides

the browser layer has two halves and neither of them needs an account, a key or a hosted
service:

- **browser-use** (MIT) is the driver. it writes python against the chrome devtools
  protocol, and it hosts nothing: it attaches to a browser that already exists (the
  chrome inside the agent's own computer) or launches a local chromium with its profile
  directory.
- **kernel-images** (Apache-2.0) is the browser, for when you want one that cannot touch
  the agent's computer. it is chrome in a container with cdp on 9222, and it is the image
  behind kernel.sh, which you build and run yourself:

```
git clone https://github.com/kernel/kernel-images
cd kernel-images/images/chromium-headful && ./build-docker.sh && ./run-docker.sh
export CLAWCREW_KERNEL_IMAGES_IMAGE=kernel-docker   # or CLAWCREW_KERNEL_CDP=ws://...
```

they compose: kernel-images gives a throwaway chrome, browser-use drives it. sessions are
rows, not side effects, so a browser with a login in it is something you can find and
close (`clawcrew browser list`, `clawcrew browser close`). their scope follows
persistence: a `workspace` agent keeps its profile, a `task_state` agent gets a temp
directory that goes away with the task.

there is deliberately no cloud path. the library's own default in 1.0 is a hosted browser
behind an api key; we never take it, and if the installed version cannot be given a local
browser the error says which version to pin rather than quietly shipping the session
somewhere else.

## getting it

running instructions live in `get/`, one folder per entry point. each one says what
works today and what does not.

| entry point | folder | status |
| --- | --- | --- |
| docker compose, uvicorn, celery | `get/https` | works |
| http api, curl, openapi ui | `get/api` | works |
| python package, embedded use | `get/sdk` | works |
| npm client | `get/npm` | not built |
| mcp server | `get/mcp` | not built |

- no api keys are needed for the demo. the deterministic model runs the real
  orchestrator, the real sandbox and the real review gate, it just does not call an llm
- `CELESTO_API_KEY` or `E2B_API_KEY` switches that same code path onto real compute
- `CLAWCREW_LLM_BASE_URL` switches the same code path onto a real model
- deployment detail, including the celesto OSS runtime on your own machine and your
  n8n and dify, is in `SELF_HOSTING.md`
- the database and the per-agent credentials story is in `docs/backend.md`
- `make check` and `python -m clawcrew.cli demo` stay where they were, for the
  impatient

## api

| route | what it does |
| --- | --- |
| `POST /crews` `GET /crews` `GET/PATCH /crews/{id}` | crews |
| `POST /crews/{id}/agents` `GET .../agents` `PATCH /agents/{id}` | agents |
| `POST /agents/{id}/computer` `GET /agents/{id}/computer` | persistent computer |
| `GET/PUT /crews/{id}/context` | shared memory |
| `POST /crews/{id}/tasks` `GET .../board` `POST /tasks/{id}/move` | kanban |
| `POST /tasks/{id}/dispatch` `POST /crews/{id}/dispatch` | run work |
| `GET /runs/{id}` `GET /tasks/{id}/runs` `GET /tasks/{id}/events` | inspection |
| `POST /a2a` `GET /crews/{id}/a2a/review-queue` `POST /a2a/{id}/review` | a2a gate |
| `GET /agents/{id}/inbox` `GET /threads/{id}` | approved messages only |
| `POST /crews/{id}/workflows` `POST /workflows/{id}/run` | workflow dag |
| `POST /workflow-runs/{id}/resume` | decide a human gate |
| `POST /crews/{id}/schedules` `POST /ops/tick` `POST /ops/process` | scheduling |
| `POST /crews/{id}/channels` `GET .../channels` `GET /channels/{id}/board` | rooms and their boards |
| `POST /channels/{id}/messages` `GET .../messages` `POST /channels/{id}/tasks` | a room's talk and work |
| `POST /crews/{id}/departments` `GET /crews/{id}/org-chart` `POST /tasks/{id}/route` | the hierarchy |
| `POST /agents/{id}/escalate` | a member pushing a decision up the line |
| `GET /agents/{id}/pov` `GET .../board` `GET/POST .../calendar` `GET /crews/{id}/roster` | point of view |
| `POST /crews/{id}/triggers` `GET .../triggers` `POST /triggers/{id}/enable` | event rules |
| `POST /crews/{id}/shift-plans` `GET /crews/{id}/coverage` `GET /shift-plans/{id}/schedule` | who is on duty, and when |
| `POST /shift-plans/{id}/roll` `POST /handoffs/{id}/accept` `GET /agents/{id}/briefing` | shift changes |
| `POST /crews/{id}/workflows/propose` `POST /workflows/{id}/approve` `POST /workflows/{id}/publish` | agents write, humans approve |
| `GET /agents/{id}/provenance` `GET /agents/{id}/audit/verify` `GET /crews/{id}/audit` | the provenance chain |
| `GET /crews/{id}/a2a/transcript` `GET /a2a/threads/{id}` | a2a as sentences, with envelopes |
| `GET /ui/pov` | the served point-of-view switcher |
| `GET /agents/{id}/profile` `PUT /agents/{id}/profile/{ns}/{key}` `GET /agents/{id}/effective` | per-agent config, and what it shadows |
| `GET/POST /agents/{id}/memory` `GET/POST/DELETE /agents/{id}/grants` | memory and provider grants, per agent |
| `PUT /agents/{id}/spawn-policy` `POST /agents/{id}/spawn` `GET /agents/{id}/lineage` | subagents, when allowed |
| `GET /crews/{id}/isolation` | the anti-mush report |
| `GET /health` `GET /providers` `GET /services` | ops, and which self-hosted tools are wired |

`/docs` is the live openapi ui.

## configuration

every knob is an env var (see `.env.example`).

| var | meaning |
| --- | --- |
| `CLAWCREW_DB` | sqlite path |
| `CELESTO_API_KEY` | persistent computers; falls back to local when unset |
| `E2B_API_KEY` | ephemeral sandboxes; falls back to local when unset |
| `CLAWCREW_ALLOW_LOCAL_FALLBACK` | `false` makes a missing key a hard error |
| `CLAWCREW_BROKER` | `inproc` (default) or `celery` |
| `CELERY_BROKER_URL` `CELERY_QUEUE` | celery wiring |
| `CLAWCREW_LLM_BASE_URL` `CLAWCREW_LLM_API_KEY` `CLAWCREW_LLM_MODEL` | any openai-compatible endpoint |
| `CLAWCREW_API_TOKEN` | shared bearer token; `/health` stays open |
| `AGENT_VAULT_PROXY_URL` `AGENT_VAULT_URL` | brokered credentials instead of keys in the environment |
| `CLAWCREW_GATE_SECRET` `CLAWCREW_PUBLIC_BASE_URL` | signs approval links, and the host they point at |
| `SUPERSERVE_API_KEY` | the one target whose runtime is not yours; opt in |
| `CLAWCREW_REVIEW_MODE` | default review mode for new crews |
| `CLAWCREW_MAX_PARALLEL` | concurrent dispatches per `dispatch_ready` |

## scaling

one process is enough for a small install: `uvicorn` for http, `clawcrew work` in a
loop for the queue, `clawcrew tick` on a timer. to scale, point
`CLAWCREW_BROKER=celery` at your existing cluster and run
`celery -A clawcrew.work.schedule.jobs worker -Q clawcrew`. the sqlite queue stays the source of
truth, so moving to celery changes who executes a job, never what a job means.

## tests

```bash
cd backend && python -m unittest discover -s tests -p "test_*.py"
```

## verified on a clean machine

not "passes on my laptop": a disposable linux VM, fresh clone, nothing preinstalled:

```bash
git clone https://github.com/arsoninstigator/clawcrew && cd clawcrew
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
make check            # vendor audit + suite
python -m clawcrew.cli demo
python -m clawcrew.cli serve
```

last run (2026-11-11, from a `git clone` of the published repo, python 3.11): install
clean, `make check` 533 tests OK, `/health`, `/providers`, `/services` answering, a crew
created over http, `demo` completing a task through the review gate, and the merge that
brought gates, the brief, connectors and brokered credentials onto one app. that run is also what caught a missing dependency:
`python-dateutil` was imported by the vendored e2b client and never declared, so
the suite only passed where it happened to be installed already.

that vm had no nested virtualization, so celesto's microVMs cannot boot there. the
persistent-computer path needs a host with `/dev/kvm`; the build check says so
instead of pretending.

533 tests, no network, no sleeps. they cover the sandbox lifecycles (both adapters
against their real generated clients), the transition table, the a2a gate (pending
must not reach an inbox) plus a2a schema conformance, cron math, queue claiming,
workflow validation and gates, the n8n and dify importers, the n8n and dify
self-hosted clients and their workflow nodes, the celesto OSS CLI adapter, channel
boards, department routing and escalation, the pov projection, the trigger engine and
the celery beat, shift rotation and handoffs, the hash-chained audit trail, the a2a
human rendering, agent-authored workflows with their approval gate, per-agent profile
isolation and spawning, agentids and signed attestations (including a tampered row and
a forged signature), multi-crew membership, specializations, the persistence switch and
its refusal to hand a task-state agent a computer, the browser layer with both lanes and
a hosted provider being refused, subagent ids with fan-out and fan-in, sagas and polls as
job kinds, and the six agent import formats, plus the http surface including auth.

## honest limits

- **local provider is not a security boundary.** it runs through the host shell with
  a binary allowlist. real isolation means celesto/e2b keys, and `isolation:
  "ephemeral"` on untrusted work.
- **self-hosted is the default, and it is a real dependency.** persistent computers
  need the celesto runtime installed on the machine that runs agents
  (`celesto doctor --strict`); without it the provider refuses instead of silently
  running agent commands on the host. n8n and dify nodes need your instances and
  their api keys, which `/services` reports honestly.
- **the hosted adapters are unverified against the live services.** they run on the
  vendors' own generated clients, so the shapes cannot be wrong the way a hand-typed
  copy can, but no key was ever pointed at celesto or e2b from this repo: the tests
  replay recorded request/response shapes through a mock transport. the first run
  with real keys is still a real run. `e2b` command execution additionally needs
  `pip install -e ".[e2b]"`, because commands go over envd, not the sandbox REST api.
- **sqlite, single node.** deliberate for v1. the `Store` surface is narrow, so
  postgres is a reimplementation of one class.
- **one shared token, no users.** the backend expects to sit behind your own auth
  proxy; multi-tenant scoping is a real design decision, not a bug fix.
- **no canvas yet.** workflow definitions are the contract for one; nothing in the
  engine assumes a specific editor.
