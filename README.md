# clawcrew

open backend for **crews of specialist agents** that actually run things: each
agent has its own computer, each crew has a shared kanban and a shared memory, all
agent-to-agent traffic passes through a review gate, and workflows are a json dag
you can edit in a canvas.

it is the backend half of a symphony/naïve style product: teams of agents with
real compute, not a chat wrapper. no frontend lives here yet, but every surface the
frontend needs is an http route.

```
crew ──┬── agents ── computers (persistent)      ── runs ── kanban cards
       ├── kanban board + append-only event log
       ├── shared context store
       ├── a2a log with a review gate
       ├── workflows (json dag: agent_task, shell, set_context, human_gate, branch)
       └── schedules (interval or cron) ── durable job queue
```

## quickstart

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"     # fastapi, uvicorn, httpx, attrs. no api keys, no docker

python -m clawcrew.cli demo           # seed, run a task, run a workflow, review a message
python -m clawcrew.cli seed           # a realistic crew you can poke at
python -m clawcrew.cli inspect        # boards, review queues, context keys
python -m clawcrew.cli serve          # http on :8000, docs at /docs
```

for the deployment picture, including the celesto OSS runtime, your own e2b, and
plugging in your n8n and dify, read `SELF_HOSTING.md`.

no api keys are needed to run the demo: the deterministic model runs the real
orchestrator, real sandbox and real review gate, it just does not call an llm. adding
a `CELESTO_API_KEY` or `E2B_API_KEY` switches the same code path onto real compute,
and `CLAWCREW_LLM_BASE_URL` switches the same code path onto a real model.

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

## concepts

**crew** (`teams`) a named group with a mission, a review mode (`reviewed` or
`open`), an orchestration policy, its own kanban columns, its own context.

**agent** a specialist: role, skills, system prompt, a runtime (persistent computer
or ephemeral), tools. three seed agents are enough to feel like a team: a
researcher who cites, a writer who only writes from crew research, an operator who
ships.

**computer** a persistent sandbox per agent, provisioned once and reused. the
agent's files and installed tooling survive between tasks. this is the celesto
lifecycle, and by default it is celesto's OSS runtime on your own machine
(`pip install celesto`, named microVMs, no account). see `SELF_HOSTING.md`.

**task** a card. moving it is validated against a transition table, and every move
writes an event, so the board is an audit log with a current-state projection.

**run** one dispatch of one task by one agent: provider, sandbox id, per-turn
steps, exit codes, stdout. runs are rows, not logs in a file, so you can query them.

**a2a** every agent-to-agent message is a row with sender, recipient, task, thread.
in a `reviewed` crew a message is `pending` and `inbox()` does not return it until
someone approves it. that is the whole point: an unreviewed claim cannot change
another agent's behaviour. on the wire it is spec-shaped a2a json over json-rpc
(`/a2a/rpc`, agent cards at `/agents/{id}/card`).

**channel** a room inside a crew: members, a conversation, and **its own board**. a
task carries a `channel_id`, so `#growth` and `#support` keep separate kanbans while
the crew board still shows everything. rooms are flat on purpose.

**department** the hierarchy, kept explicit: one coordinator, members reporting to it,
departments that nest. routing a card to a department puts it on the coordinator's
desk, and escalation walks up the reporting line as a gated a2a message. hierarchy for
accountability, channels for conversation.

**pov** the workspace as one agent sees it: their board, their rooms, their calendar
(derived from explicit blocks, schedule targets and card due dates), their inbox,
their computer, the context they share, and only what needs attention. switching pov
is a projection, never a login, which is why it is cheap and honest.

**trigger** when this happens, queue that. `event` + exact-match `condition` +
`action` + `payload` with `${path}` placeholders. card moves, room messages, finished
workflows and the nightly beat all reach the same engine, and the queue is celery's.

**workflow** a dag of seven node types, validated on save (no cycles, no dangling
deps), executed with a `human_gate` that pauses the run until decided. two of the
seven hand a step to a tool you already run: `n8n` posts to a webhook on your n8n,
`dify` runs a workflow on your dify.

**schedule** interval or 5-field cron, durable, feeding the same job queue the api
uses. `tick()` is idempotent; the queue claim is a single serialized update so two
workers never take the same job.

## api

| route | what it does |
| --- | --- |
| `POST /crews` `GET /crews` `GET/PATCH /crews/{id}` | crews |
| `POST /crews/{id}/agents` `GET .../agents` `PATCH /agents/{id}` | agents |
| `POST /agents/{id}/computer` `GET /agents/{id}/computer` | persistent computer |
| `GET/PUT /crews/{id}/context` | shared memory |
| `POST /crews/{id}/tasks` `GET .../board` `PATCH /tasks/{id}/move` | kanban |
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
| `CLAWCREW_REVIEW_MODE` | default review mode for new crews |
| `CLAWCREW_MAX_PARALLEL` | concurrent dispatches per `dispatch_ready` |

## scaling

one process is enough for a small install: `uvicorn` for http, `clawcrew work` in a
loop for the queue, `clawcrew tick` on a timer. to scale, point
`CLAWCREW_BROKER=celery` at your existing cluster and run
`celery -A clawcrew.jobs worker -Q clawcrew`. the sqlite queue stays the source of
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

last run (2026-11-10, ubuntu 24.04, python 3.12, 2 vcpu): clone of the repo,
install clean, `make check` 227 tests OK, server up, `/health`, `/providers`,
`/services` answering, a crew created over http, `demo` completing a task through
the review gate. that run is also what caught a missing dependency:
`python-dateutil` was imported by the vendored e2b client and never declared, so
the suite only passed where it happened to be installed already.

that vm had no nested virtualization, so celesto's microVMs cannot boot there. the
persistent-computer path needs a host with `/dev/kvm`; the build check says so
instead of pretending.

260 tests, no network, no sleeps. they cover the sandbox lifecycles (both adapters
against their real generated clients), the transition table, the a2a gate (pending
must not reach an inbox) plus a2a schema conformance, cron math, queue claiming,
workflow validation and gates, the n8n and dify importers, the n8n and dify
self-hosted clients and their workflow nodes, the celesto OSS CLI adapter, channel
boards, department routing and escalation, the pov projection, the trigger engine and
the celery beat, and the http surface including auth.

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
