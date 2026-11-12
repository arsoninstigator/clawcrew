# the open source landscape

crittertown is byoc and byok, which only means something if the pieces it stands on are
yours too. so: what we actually use, what we only talk to, and what looked good and is
not usable yet.

the rule we hold to: **adapt code, never call a cloud api for the core loop.** a provider
sdk may appear as an *option* (persistent cloud, ephemeral cloud), never as the only path.

## what we adapt

### persistent, persistent computers

- repo: `PersistentAI/persistent`, apache-2.0
- what it is: a lightweight vm per agent, about 500ms to start, keeps files and state
  between sessions, runs code, a browser, or a desktop.
- how we use it: `crittertown/create/sandbox/persistent_local.py` drives the `persistent` cli for
  the local runtime (qemu on the host). `persistent.py` is the cloud path, opt in, and it is
  the only place a persistent api key matters.
- what we do not use: their python sdk at runtime in the local path, because the cli is
  the stable surface and does not drag a dependency into the core.
- host need: qemu for local. no nested virtualisation needed, which is why it works on a
  vps where firecracker does not.

### ephemeral runtime, disposable sandboxes

- repo: `ephemeral-dev/runtime`, apache-2.0, go. (this is the repo formerly known as `infra`.)
- what it is: the whole ephemeral stack. control plane, per node orchestrator driving
  firecracker, the in vm agent, the edge router, and the template builder. "creating a
  sandbox" is restoring a snapshot, which is why it is fast.
- how we use it: `crittertown/create/sandbox/ephemeral.py` talks the api when you have a key.
  `crittertown get disposable --target ephemeral --apply` uses their **embed** package (two files,
  one `docker compose up`) when you would rather run it yourself.
- host need: `/dev/kvm`. embed is one machine by design. their repo also ships terraform
  for aws, gcp and azure and a kubernetes manifest, which is the same shape our cloud
  targets emit.
- why disposable at all: an agent that can run code should have somewhere to do it that
  is already gone when the task ends. persistent computers are for the agent, disposable
  sandboxes are for the risky step.

## what we only talk to

n8n and dify are source available, not mit or apache. so we never copy their code. we
import their **export formats** (`orgchart/authoring/importers.py`) and call **your own
instance** over their published api (`create/workflow/services/`). that separation is why
they are two files and not a dependency.

### hosted, opt in

- repo: `hosted-ai/hosted`, apache-2.0. firecracker microvms with desktop
  control and preview ports, plus a console and docs.
- what the repo actually contains: the typescript and python sdks, the cli, the console
  and the docs. the runtime is their cloud, so this is the one target that is **not**
  byoc. saying otherwise would be a nicer story and a false one.
- how we use it: `crittertown/create/sandbox/hosted.py` drives the published python sdk,
  and only documented calls: `Sandbox.create`, `commands.run`, `files.write`,
  `files.read_text`, `kill`, `pause`, `get_signed_preview_url`. `pip install hosted`
  is the optional extra, and without it the provider refuses with that exact hint.
- why it is still worth having: desktop control and signed preview links are how a human
  looks at what an agent built, and `crittertown get ... --target hosted` is one command.

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
- what we built against it: `crittertown/create/coordination/` ships the contract, not a
  pretend client. `CoordinationBackend` is `claim`, `release`, `append`, `read`, and
  `SqliteCoordination` is the reference implementation, tested, used by the app today.
  `plan_runtime` writes the values file their chart expects and plans the helm install,
  and `actor_contract()` records which actor holds which primitive (`CrewBoard` maps to
  `claim`, `AgentInbox` and `ChannelRoom` map to `append`/`read`).
- what is not done: the transport. no generated client, no websocket client, no fake
  endpoints. the seam is one class, and `crittertown coordinator --public-url ... --apply`
  is how the runtime gets there.
- the local dev path is `bunx durable-actors dev`, which is how the actor definitions can
  be exercised without a cluster.

### considered and not taken

**deno celld** and **rivet** are both real self hostable durable object runtimes. not
chosen because terse's actor model matches the primitives crittertown needs more directly
and ships a helm chart; celld stays the fallback if an actor per board ever stops being
enough.

**kubernetes** - we emit manifests rather than embedding a client. a pod is a fine
computer; a control loop is not something this repo should own.

## licences of what is vendored

`crittertown/vendor/` holds the generated persistent and ephemeral api clients, unmodified, each with
its `LICENSE` and a `PROVENANCE.md` naming upstream and the commit. `make vendor-audit`
checks that every vendored folder still has both, so a copy cannot arrive without its
licence.

`crittertown/thirdparty.md` is the summary table: copied, adapted, or spoken to.
