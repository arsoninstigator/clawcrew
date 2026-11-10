# the open source landscape

clawcrew is byoc and byok, which only means something if the pieces it stands on are
yours too. so: what we actually use, what we only talk to, and what looked good and is
not usable yet.

the rule we hold to: **adapt code, never call a cloud api for the core loop.** a provider
sdk may appear as an *option* (celesto cloud, e2b cloud), never as the only path.

## what we adapt

### celesto, persistent computers

- repo: `CelestoAI/celesto`, apache-2.0
- what it is: a lightweight vm per agent, about 500ms to start, keeps files and state
  between sessions, runs code, a browser, or a desktop.
- how we use it: `clawcrew/create/sandbox/celesto_local.py` drives the `celesto` cli for
  the local runtime (qemu on the host). `celesto.py` is the cloud path, opt in, and it is
  the only place a celesto api key matters.
- what we do not use: their python sdk at runtime in the local path, because the cli is
  the stable surface and does not drag a dependency into the core.
- host need: qemu for local. no nested virtualisation needed, which is why it works on a
  vps where firecracker does not.

### e2b runtime, disposable sandboxes

- repo: `e2b-dev/runtime`, apache-2.0, go. (this is the repo formerly known as `infra`.)
- what it is: the whole e2b stack. control plane, per node orchestrator driving
  firecracker, the in vm agent, the edge router, and the template builder. "creating a
  sandbox" is restoring a snapshot, which is why it is fast.
- how we use it: `clawcrew/create/sandbox/e2b.py` talks the api when you have a key.
  `clawcrew get disposable --target e2b --apply` uses their **embed** package (two files,
  one `docker compose up`) when you would rather run it yourself.
- host need: `/dev/kvm`. embed is one machine by design. their repo also ships terraform
  for aws, gcp and azure and a kubernetes manifest, which is the same shape our cloud
  targets emit.
- why disposable at all: an agent that can run code should have somewhere to do it that
  is already gone when the task ends. persistent computers are for the agent, disposable
  sandboxes are for the risky step.

### buzz, and the nostr room protocol

- repo: `block/buzz`, apache-2.0 (this repository is buzz 0.x; `block/buzz-app` is 1.0).
- what it is: a self-hostable workspace where humans and agents share rooms, built as a
  nostr relay. every message, reaction, workflow step, review approval and git event is a
  signed event in one log, and an agent is a member with its own key rather than a bot with
  a permission flag.
- how we use it: **the protocol, not the client.** `clawcrew/comms/` speaks nip-01 (event
  ids, tags, bip340 signatures) and nip-29 (rooms as groups, kind 9 messages with an `h`
  tag, 9000/9001 for roster changes). pointing a room at a buzz relay is
  `clawcrew comms bridge <room> buzz --relay wss://...`, which is the whole integration,
  because their relay already speaks the format we write.
- what we do not adopt: their scope. buzz is shaped around software teams (patches, ci,
  review, merges in the room). a support shift or a research room is the same substrate and
  not the same product, and copying their event taxonomy would have imported the first into
  the second.
- not vendored: it is rust, it is large, and there is nothing in it that the protocol does
  not already give us.

### octo, the closest reference for a full workplace

- repos: `Mininglamp-OSS/octo-server` (go backend, apache-2.0), `octo-web`, `octo-lib`,
  `octo-channel-gateway`, `octo-cli`, `octo-adapters`, plus per-platform clients.
- what it is: a self hosted workplace where agents are first class conversation
  participants, with rest and websocket apis, an agent runtime ("lobster"), and a real im
  core underneath. the closest thing to the shape clawcrew's channels are aiming at, and
  apache-2.0 like us.
- what we took: the lesson, not the code: an agent is a member with a session and an
  identity, membership is per room, and the gateway that bridges external im products into
  the workplace is a separate service rather than something the core does inline.
- what is not wired: an adapter. their bot surface is long-poll based (`docs/bot-events-
  longpoll.md` upstream), which is a real piece of work rather than a mapping, so
  `Transports` ships the two-call http contract instead and this is how octo would plug in.
  shipping a guessed client against a long-poll api would have been the fake version.
- note: their client and server are go and typescript; nothing here could be ported into a
  python backend without being rewritten.

## the products this is aimed at

these are references for the shape of the product, not code sources. none of them is
open source in a way that lets us copy anything, and nothing below was copied.

- **ando** (`ando.so`): agents as true participants, context that compounds inside the
  room, human attention as the scarce resource. the third one is why presence is ephemeral
  in clawcrew rather than a stored table.
- **kylon** (`kylon.io`): a room that produces artifacts (trackers, boards) rather than only
  messages, and external agents (claude code, codex) joining as members.
- **paperclip** (`paperclip.ing`): the org chart as the interface, agents hired into roles
  with budgets and goals. that is the layer above comms, and it is what
  `orgchart/` and `departments.py` are for.
- **cua spaces** (`cua.ai`) and **vorflux**: an always-on computer per agent that a human
  watches and can take over. in clawcrew that is `get persistent`, and a room is where you
  see what the agent did with it.
- **symphony** and other agent-team products: the team is the unit of work, not the single
  run. that is the whole premise, and none of their code is public.

## what we only talk to

n8n and dify are source available, not mit or apache. so we never copy their code. we
import their **export formats** (`orgchart/authoring/importers.py`) and call **your own
instance** over their published api (`create/workflow/services/`). that separation is why
they are two files and not a dependency.

### superserve, opt in

- repo: `superserve-ai/superserve`, apache-2.0. firecracker microvms with desktop
  control and preview ports, plus a console and docs.
- what the repo actually contains: the typescript and python sdks, the cli, the console
  and the docs. the runtime is their cloud, so this is the one target that is **not**
  byoc. saying otherwise would be a nicer story and a false one.
- how we use it: `clawcrew/create/sandbox/superserve.py` drives the published python sdk,
  and only documented calls: `Sandbox.create`, `commands.run`, `files.write`,
  `files.read_text`, `kill`, `pause`, `get_signed_preview_url`. `pip install superserve`
  is the optional extra, and without it the provider refuses with that exact hint.
- why it is still worth having: desktop control and signed preview links are how a human
  looks at what an agent built, and `clawcrew get ... --target superserve` is one command.

### terser's durable actors, the durable object layer

- repo: `TerseAI/durable-actors`, mit, rust plus sqlite, with python and typescript
  runtimes and generated clients.
- what it is: an open source stand in for cloudflare durable objects. a class with
  **durable state** and **serialized execution**, driven over websockets, with
  observability built in. exactly the two properties a crew board and an agent inbox need.
- how it is self hosted: helm chart on gke with gvisor sandbox nodes, postgres for the
  control plane and one standard gcs bucket for actor state, controller on `:7100` behind
  your ingress. create `actors-credentials` with `postgres-url`, `jwt-signing-key` and
  `api-key` first. an invoke-only key is exposed as `DURABLE_ACTORS_INVOKE_API_KEY`, which
  is the key a backend like this one should get.
- what we built against it: `clawcrew/create/coordination/` ships the contract, not a
  pretend client. `CoordinationBackend` is `claim`, `release`, `append`, `read`, and
  `SqliteCoordination` is the reference implementation, tested, used by the app today.
  `plan_runtime` writes the values file their chart expects and plans the helm install,
  and `actor_contract()` records which actor holds which primitive (`CrewBoard` maps to
  `claim`, `AgentInbox` and `ChannelRoom` map to `append`/`read`).
- what is not done: the transport. no generated client, no websocket client, no fake
  endpoints. the seam is one class, and `clawcrew coordinator --public-url ... --apply`
  is how the runtime gets there.
- the local dev path is `bunx durable-actors dev`, which is how the actor definitions can
  be exercised without a cluster.

### considered and not taken

**deno celld** and **rivet** are both real self hostable durable object runtimes. not
chosen because terse's actor model matches the primitives clawcrew needs more directly
and ships a helm chart; celld stays the fallback if an actor per board ever stops being
enough.

**kubernetes** - we emit manifests rather than embedding a client. a pod is a fine
computer; a control loop is not something this repo should own.

## licences of what is vendored

`clawcrew/vendor/` holds the generated celesto and e2b api clients, unmodified, each with
its `LICENSE` and a `PROVENANCE.md` naming upstream and the commit. `make vendor-audit`
checks that every vendored folder still has both, so a copy cannot arrive without its
licence.

`THIRD_PARTY.md` is the summary table: copied, adapted, or spoken to.
