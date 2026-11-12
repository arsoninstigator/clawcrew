# what is left, as a spec

nothing in this file is implemented. it is the contract for the next build, written
down so it can be argued with before there is code to argue about.

it settles three things: what "left" actually means after the last push, the api
shapes each remaining piece will expose, and what counts as done for each one.

---

## 0. the two things asked about: are they baked in?

short answer to both: yes, and in both cases the missing part is smaller than it
sounds, but it is a real part.

### schedule and the overnight job

baked:

- `work/jobs.py` registers five schedule tasks: `run_job`, `tick`, `nightly`,
  `fire_event`, `process`.
- beat runs two schedules: `crittertown-tick` every 15s (enqueues every due row in
  `schedules`) and `crittertown-nightly` at `crontab(hour=2, minute=0)` with
  `schedule.conf.timezone = "UTC"`.
- `nightly()` does one thing: `triggers.fire("schedule.nightly", {"team_id": "",
  "at": now()})`. it does not hardcode work; overnight behavior is a rule that can be
  edited at runtime.
- `docker-compose.yml` runs `api`, `redis`, `worker` (`--concurrency=2`) and `beat`.
  schedule is a core dependency in `pyproject.toml`, not an extra.
- the sqlite `job_queue` is the source of truth and redis only carries a job id, so a
  worker that dies mid-job leaves a `running` row that can be inspected and requeued
  instead of losing the work. `claim()` is a single update, so two workers cannot
  take the same row.

the honest caveat: a fresh install has zero `schedule.nightly` rules, so nothing
happens at 2am except due `schedules`. the mechanism is live and tested; the default
behavior is not shipped. that is what section 5 fixes.

### the flows

baked, as engine, gates and bindings:

- `work/workflows.py` stores a json dag and runs it. seven node types:
  `agent_task`, `shell`, `set_context`, `human_gate`, `branch`, `n8n`, `dify`.
- validation is strict at save time: unknown type, missing node spec, dangling
  dependency, cycle, or an `n8n` node without a webhook path are all 422s at save,
  not failures at 3am.
- `human_gate` creates a card in `review` and parks the run until
  `POST /workflow-runs/{run_id}/resume`.
- `${path}` templating resolves against run state, so one node's output feeds the
  next.
- authoring has a gate: agents `propose` (coauthors recorded), agents `review`, and
  only a human `approve`s. `publish` then binds a cron or interval, and the beat runs
  it without anyone touching it again.
- `n8n` and `dify` are called on your own instances, never copied into this repo, and
  `GET /services` reports whether each is wired.

not baked: the canvas that edits the definition, and the nightly pack of rules. both
below.

---

## 1. the front door, one outcome in

**purpose.** one sentence in, a staffed plan out. every entry point today is
administrative: `POST /crews/{id}/tasks`, then `plan`, then `dispatch`. the front door is the
missing conversational intake: state an outcome, it gets torn into cards, placed in
channels and departments, assigned, and optionally dispatched.

**route.** `POST /crews/{team_id}/ask`

```json
{
  "text": "get the launch page live and the announcement out by friday",
  "mode": "plan",
  "channel_id": null,
  "due": "2026-11-13",
  "constraints": {"budget": "no new providers", "tone": "plain"}
}
```

```json
{
  "ask_id": "ask_01h...",
  "crew": {"id": "team_...", "name": "acme"},
  "interpretation": {
    "goal": "a live launch page and a sent announcement",
    "deliverables": ["launch page deployed", "announcement sent"],
    "deadline": "2026-11-13",
    "assumptions": ["announcement channel is email"],
    "questions": []
  },
  "plan": [
    {"task_id": "tsk_...", "title": "draft launch page copy", "channel": "growth",
     "department": "marketing", "assignee": "agt_...", "depends_on": [],
     "done_when": "copy approved in review"},
    {"task_id": "tsk_...", "title": "build and deploy the page", "channel": "growth",
     "department": "engineering", "assignee": null, "depends_on": ["tsk_..."],
     "done_when": "url returns 200"}
  ],
  "questions": ["no agent in this crew builds pages; assign or add one"],
  "dispatched": 0
}
```

**steps, fixed order.**

1. interpret: goal, deliverables, deadline, explicit constraints, verbatim text kept.
2. decompose: 1 to 8 cards, each with an outcome sentence and a `done_when` line.
3. place: channel (existing, or created by slug if `create_channels` is set),
   department head when the crew has departments, assignee from the same selector
   `pick_agent` uses unless the ask names someone.
4. sequence: `depends_on` edges, cycles rejected before any card is written.
5. record: the ask and its plan are stored and posted to crew context under
   `last_ask`, so plan mode is auditable, not just returned.

**storage.** new `asks` table: `id, team_id, text, mode, interpretation json, plan
json, status, created_by, created_date`. cards go through the existing kanban
service, so `task.created` and every trigger fire normally.

**invariants.**

- intake never invents an agent. if no skill matches, assignee stays null and the ask
  says so in `questions`. a plan with a hole is honest; a plan with a fake owner is
  not.
- plan mode writes cards to `backlog` only, creates zero runs.
- dispatch mode still respects review mode, `human_gate`, `max_turns` and a per-ask
  cap (default 8 cards).
- the route is idempotent per `Idempotency-Key` header. a retried intake that
  duplicates eight cards is worse than a 500.
- the ask never chooses a provider or a computer; that is the runner's job.

**failure modes.** empty text 422; unknown crew 404; no matching skills 200 with
questions and no cards; llm unavailable 503 with the deterministic fallback result if
one is configured.

**tests.** plan mode creates cards in backlog and zero runs; dispatch mode dispatches
only ready cards; unmatched skill leaves assignee null and is reported; dependencies
serialize in the returned order; a replayed idempotency key makes one set of cards; a
crew in review mode gets no auto-dispatch.

**non-goals.** chat memory across asks, streaming, cross-crew asks (v2), and
re-planning an ask after the fact.

---

## 2. the morning brief

**purpose.** what ran overnight, what needs a human, what is due today, without
opening a board. note: `GET /agents/{id}/briefing` already exists but it is a shift
briefing (on-duty plans, open handoffs). it is not this.

**routes.**

- `POST /crews/{team_id}/brief` builds and stores one for a window.
- `GET /crews/{team_id}/brief?date=YYYY-MM-DD` reads the stored one.
- `GET /agents/{agent_id}/brief` that agent's slice of the same numbers.

```json
{
  "date": "2026-11-10",
  "window": {"from": "2026-11-09T18:00:00Z", "to": "2026-11-10T06:00:00Z"},
  "overnight": {"runs": 14, "ok": 12, "failed": 2, "cards_completed": 9,
                "cards_moved": 21, "triggers_fired": 4},
  "waiting": {"review": 3, "blocked": 1, "human_gate_runs": 1, "a2a_pending": 2,
              "handoffs_open": 1},
  "today": {"due": 2, "on_shift": ["agt_..."], "schedules_firing": 1},
  "risks": ["2 runs failed: tsk_..., tsk_...", "handoff open for 9h"],
  "prose": "12 of 14 overnight runs finished. two failed on the deploy card...",
  "links": {"board": "/crews/team_x/board", "review": "/crews/team_x/a2a/review-queue"}
}
```

**source of truth.** run rows, task transitions, trigger firings, the a2a pending
queue, handoff rows and schedules. the brief stores the numbers it showed (`briefs`
table: `id, team_id, date, window_from, window_to, numbers json, prose, created_date`)
so "you said two failed last tuesday" is checkable against what was shown, not
recomputed from a db that has moved on.

**generation.** a new job kind `brief` in `triggers.ACTIONS`, so `schedule.nightly`
can queue it like anything else. generation is a read plus an insert, no llm needed.

**ordering and honesty.** numbers first, prose second. if prose is generated at all,
it must cite the numbers it summarizes, and any brief whose prose disagrees with its
counts is stored with the prose dropped. with a model in the loop, that is the one
rule that keeps the brief trustworthy.

**retention.** 90 days, one row per crew per day. regenerating the same date
overwrites.

**tests.** a crew with one failed run and two review cards produces those numbers;
the window is 18:00 to 06:00 UTC by default and configurable; regenerating is
idempotent; prose that contradicts a count is discarded; an empty crew produces a
brief of zeros rather than an error.

---

## 3. canvas, part a: pov shell and the profile editor

**purpose.** `/ui/pov` is a served switcher: one page, no build step, honest proof
that pov is a projection. the product version needs three things it does not have:
real navigation, an editor for the nine namespaces, and a writable calendar.

**3a. the pov shell.**

- left rail: crews, then channels inside the selected crew.
- header: "viewing as" selector, with the agent's permanent `agent_key` shown next to
  the name, because that key is the identity and the name is a label.
- views, all fed by existing endpoints: board (agent or channel), calendar (derived
  blocks plus schedule windows plus card due dates), inbox (`GET
  /agents/{id}/inbox`, sentences with the a2a envelope beside each), profile,
  computer (`GET /agents/{id}/computer`), runs (recent runs from
  `GET /agents/{id}/pov`).

**rules.** pov is a projection over the same rows, never a session. switching changes
the filter, not the agent's permissions. everything rendered as that agent is
read-only except calendar events and card moves, which go through the same endpoints
and the same audit as any other write, so "i did it as bob" is a lie the system
cannot tell.

**3b. the profile editor.**

- one column per namespace: `skills`, `scope`, `memory`, `instructions`,
  `knowledge`, `perms`, `tools`, `vertical`, `spawn`.
- writes are `PUT /agents/{id}/profile/{namespace}/{key}`: replacement, never a deep
  merge. the editor must show the value it will store, never a merged preview,
  because a merged preview would teach the operator something false about what the
  agent gets.
- `perms` is not a profile key. grants get their own card: provider, kind
  (oauth/mcp/api), scopes, expiry, revoke.
- after each save the editor shows `fingerprint` and `GET
  /agents/{id}/consistency`, so drift is visible at write time and not in a report
  later.
- `GET /crews/{id}/isolation` gets a page of its own. two agents that drifted into
  the same one is the failure this whole layer exists to prevent.

**non-goals.** a generic chat-side editor for these nine, and any bulk edit across
agents (that is how agents become mush).

---

## 4. canvas, part b: the workflow editor

the definition is the contract, so the editor is a renderer and a validator and
nothing else. it may only produce definitions `validate_definition` accepts.

- palette: the seven node types. center: the dag. right: inspector for the selected
  node (its required fields, per type). bottom: the run pane, per-node status from
  `GET /workflow-runs/{id}`, with the `resume` control for a parked `human_gate`.
- the server is the only adjudicator: the editor saves through `POST
  /crews/{id}/workflows` or `propose`, and a cycle is a client warning plus a server
  422. no client-side bypass, no second validator.
- status ribbon: `draft`, `proposed`, `active`, `archived`, with `approve`, `reject`
  and `publish` visible only to a human. the gate is the point of the feature.
- `n8n` and `dify` nodes read `GET /services` and refuse to save against an instance
  that is not configured (see the open decision on import round-trips).
- `POST /crews/{id}/workflows/import` with a `n8n` or `dify` source renders what
  could not be mapped instead of silently dropping it.

---

## 5. the overnight pack

right now `schedule.nightly` fires into an empty rule set. the pack is a small,
reviewed set of rules a crew adopts in one call:

`POST /crews/{team_id}/triggers/pack` with
`{"pack": "overnight", "dry_run": true}` returns the rows it would create, with
payloads rendered, before anything is written. adopting the pack twice is a no-op by
name.

1. **nightly brief**: `schedule.nightly` -> action `brief`. the reason the night
   exists.
2. **re-dispatch idle cards**: `schedule.nightly` -> action `dispatch`, limited to
   cards idle longer than a threshold. needs a `since`/idle knob, which is new.
3. **fail loudly**: `run.failed` -> action `task`, creating a card in the `#ops`
   review column with the error text. this is the one that matters most, because a
   silent failure is the only failure that costs a whole night.
4. **roll shifts**: `schedule.nightly` -> action `schedule`, rolling any plan whose
   slot boundary passed, so nobody discovers at 9am that the rotation never advanced.
5. **a2a digest**: `schedule.nightly` -> writes pending a2a into the crew context key
   `a2a.pending`, which is an input to the brief rather than a second delivery.

what the pack needs that does not exist yet, and is therefore in scope here:

- job kind `brief` in `ACTIONS`, plus the worker branch that runs it.
- an idle-age condition for dispatch (today conditions are exact matches only).
- a delivery target for the brief, per crew. see below.

---

## 6. order of work

each step is verified on a clean vm before the next one starts:

1. overnight pack and the brief service. backend only, and it answers the only
   question that matters at 3am: did anything happen.
2. the front door `/ask`. the first entry point a non-technical operator can use.
3. canvas a: pov shell and the profile editor.
4. canvas b: the graph editor.

the gate for each: `make check` green, a fresh clone on a disposable vm, the
endpoints exercised over http against a real crew, then the sandbox terminated.

---

## 7. open decisions

1. **brief delivery.** channel message inside the crew, outbound webhook, a file on a
   computer, or email through your own instance. proposal: channel message plus the
   `GET`, no outbound by default.
2. **intake model.** `/ask` needs an llm somewhere. options: a key you supply
   (`CRITTERTOWN_LLM`), a local endpoint, or a deterministic parser in v1 with the llm
   behind a flag. proposal: deterministic first, then the key.
3. **where the canvas lives.** served page (python, no build step) or a real spa.
   proposal: keep the served page for pov, and let the graph editor be the first spa.
4. **who writes the brief prose.** template or model. proposal: template first, model
   as optional enrichment that cannot change a number.
5. **an `n8n`/`dify` node against an unconfigured instance.** save-and-flag or refuse.
   proposal: refuse in the editor, allow over the api so imports round-trip.

## as of 12 nov 2026

wired this week, with tests that hold it: gates and their signed links, the front door
(`/ask`), the morning brief, workspace connectors with supervisor grants, the coordination
contract, and brokered per-agent credentials (`ingredients/credentials/credential_broker`).

the duplicate modules are retired, and the tree has one home per thing now: code at the
root, baked-in parts under `ingredients/` named for what they do, called parts under
`toolbox/`, and the upstream names only in `crittertown/thirdparty.md`.

still open, in the order it is worth doing:

1. ~~**postgres store.**~~ done: `core/store_postgres.py` with the four methods,
   `core/dialect.py` as the single translation step, and the published ddl now checked
   against the schema by a test (it had gone stale at 34 of 45 tables, and carried an
   `autoincrement` postgres does not accept). see `crittertown/docs/backend.md`.
2. **sandbox env injection.** `CredentialBroker.sandbox_env()` is built and tested; the
   provider interface needs an `env` argument before a sandbox can actually use it.
