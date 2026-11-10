# stack decisions

what each named piece of the brief turned into, and why. the short version: the
things that are hard to get right (sandbox lifecycle, the review gate, the
transition table, cron math, queue claiming) are implemented, and the things that
are vendor choices are behind one adapter interface.

## symphony (by wix) → the crew model

symphony sells a coordinated team of 5-ish agents for a small business, sharing
context, each owning a domain (outreach, marketing, scheduling, research, finance).
the transferable part is not the agents, it is the *shape*: one shared memory, one
board, specialists with narrow jobs.

built: `teams` + `agents` + `context_entries` + a per-crew kanban whose columns come
from the crew's own policy. a crew can be forced into `reviewed` mode, which changes
both the kanban handoff and the a2a gate.

## naïve (usenaive.ai) → primitives

naïve's pitch is an "autonomous company runtime": deploy AI employees, and every
team gets a runtime plus real-world capabilities. the useful framing is that an
agent is *runtime + capabilities + job*, and that the runtime is durable.

built: `runtime` json on each agent (`computer`, `provider`, `image`,
`timeout_seconds`), provisioned by `ensure_computer` and reused. capabilities are
`skills` + `tools` on the agent and `required_skills` on the task, matched by the
selector.

## celesto → persistent computers

an agent's computer is created once and kept: files, packages and browser profile
survive between tasks. that persistence is what makes "the writer remembers the
brief it wrote last week" real rather than prompt stuffing.

built: `sandbox/celesto.py` behind `SandboxProvider`, plus `computers` rows keyed by
agent. `LocalProvider` reproduces the same lifecycle on disk for development.

## e2b → ephemeral sandboxes

anything an agent might break runs in a throwaway box. task-level
`isolation: "ephemeral"` sends the run to e2b instead of the agent's computer, and
the sandbox is destroyed in a `finally` block whether the run succeeded or raised.

built: `sandbox/e2b.py`, `Handle.kind == "ephemeral"`, destroy-on-exit. the test
suite asserts the directory is gone after the run.

## zop.dev / celery → scheduling

kept the semantics in sqlite (`schedules` + `job_queue`) and made the transport
swappable. `InProcBroker` is the default and needs no infra; `CeleryBroker` pushes
job ids to an existing cluster. cron is a real 5-field matcher (`*/15`, ranges,
lists, dow) with a bounded search, not a cron-syntax library.

why not celery beat directly: beat owns its own schedule store, which forks the
source of truth. one table, one truth, any number of workers.

## orchestrator.inc / a2a → the message bus

a2a here is not a protocol claim, it is a schema: `from`, `to`, `task`, `thread`,
`kind`, `body`, `status`, `reviewed_by`. the deliberate addition over "just send the
message" is the **gate**: in `reviewed` mode a message is `pending` and the
recipient's inbox does not include it. telemetry-style "you can read it later"
review is not review.

## vibe-kanban / agent-kanban → board semantics

took the parts that make agent boards usable and skipped the ui: per-crew columns,
a validated transition table (no `backlog → done`), an append-only event log per
card, and a `review` column that is a real gate rather than decoration.

## n8n / langflow / dify → workflow definitions

the definition is `{nodes: [...]}` with five types and a `depends_on` graph. saving
validates duplicates, dangling deps, unknown types and cycles. execution is
topological, supports `${trigger.x}` / `${node.field}` templating, pauses on
`human_gate`, and resumes from the exact card that blocked it.

that json is the integration point: an n8n-style canvas renders and edits it, this
engine runs it. no lock-in in either direction.

## ando.so / buzz (block) → agent-only channels

not wired as transports. what they imply is that agent conversation is a first-class
channel with its own retention and review story, so messages are their own table
with their own status lifecycle instead of being folded into task comments. swapping
in an external agent-messaging transport means implementing `send` and `inbox`.

## what is deliberately not here

- no frontend, no canvas, no auth provider. this is the backend the canvas will call.
- no llm in the orchestrator. planning stays cheap and reproducible; the model does
  work inside a run, it does not decide the graph.
- no vector store. shared context is key-value with full history, which is enough
  until a crew's context stops fitting in a prompt.
