# self-hosting

clawcrew is built to run on machines you own, against software you run. nothing in
the default path calls a vendor's hosted api and nothing depends on an account.

## compute: persistent OSS, locally

(needs nested virtualization, so this runs on a real host, not inside a sandbox)

the `persistent` lifecycle is persistent's own runtime on your host:

```bash
pip install persistent
persistent setup          # downloads the microVM tooling (qemu on macOS, kvm on linux)
persistent doctor --strict # refuses to pretend if virtualization is not usable
```

clawcrew then drives it through the documented CLI with `--json`
(`clawcrew/sandbox/persistent_local.py`): `computer create --name`, `exec --start`,
`start`/`stop`/`pause`/`resume`/`delete`/`info`/`list`. an agent's computer is a
named microVM that survives between tasks.

the hosted api is still supported, but it is opt-in: `PERSISTENT_MODE=cloud` plus
`PERSISTENT_API_KEY`. that switch exists so the same code can run either side, not
because it is the default.

the hard requirement is **nested virtualization**: `persistent doctor --strict` must
pass. a plain cloud sandbox (tenki, ephemeral, most CI runners) gives you a VM without
`/dev/kvm`, so persistent's microVMs cannot boot there. that is not a clawcrew
limitation, it is what a microVM needs. run persistent computers on a host that has
it, and use the disposable sandboxes for build and test work.

in containers: a microVM needs `/dev/kvm`. build with
`--build-arg INSTALL_PERSISTENT=true` and map the device, as the comment in
`docker-compose.yml` shows. for most deployments the worker runs on the host and the
api runs in the container, which needs nothing extra.

## compute: ephemeral, self-hosted

ephemeral's runtime is Apache-2.0 and self-hostable. point clawcrew at it:

```bash
EPHEMERAL_BASE_URL=http://localhost:3000   # your instance's api
EPHEMERAL_API_KEY=<the key your deployment generates>
pip install -e ".[ephemeral]"              # exec goes over envd, which is their sdk's hop
```

the sandbox lifecycle (create/get/pause/resume/connect/delete) runs on ephemeral's own
generated client, vendored in `clawcrew/vendor/ephemeral_api`. command execution goes over
envd rather than the REST api, so it needs their sdk; the adapter says exactly that
instead of inventing a route.

## workflow tools: n8n and dify, yours

these are **plugged in, not copied**. both projects are source-available with
licences that forbid redistribution here, and both already expose an api on your own
instance, so that is the seam:

```bash
# n8n: one container, then create an api key in Settings > n8n API
docker compose --profile workflows up -d
export CLAWCREW_N8N_BASE_URL=http://localhost:5678
export CLAWCREW_N8N_API_KEY=...

# dify: run their compose (api, worker, web, postgres, redis, vector store), then
# create an app key under API Access
export CLAWCREW_DIFY_BASE_URL=http://localhost/v1
export CLAWCREW_DIFY_API_KEY=app-...
```

with those set, two workflow node types delegate work to them:

```json
{"id": "digest", "type": "n8n", "path": "nightly-digest", "payload": {"topic": "${seed}"}}
{"id": "reply", "type": "dify", "inputs": {"draft": "${seed}"}, "response_mode": "blocking"}
```

`n8n` posts to the workflow's webhook (`/webhook/<path>`, or `/webhook-test/<path>`
with `"test": true`) and records whatever the webhook answers. `dify` calls
`POST /v1/workflows/run` and records the `data` block, failing the node when dify
reports `failed`/`stopped`. the n8n client also manages workflows over
`/api/v1` (list, get, create, activate, deactivate, executions) and can export one
into the shape our importer already reads.

their *file formats* are imported separately (`clawcrew/importers.py`), because an
import reads a json file you exported and a node calls a daemon you run. both paths
are documented in `THIRD_PARTY.md`.

## what is still someone else's cloud

nothing required. optional: a hosted llm endpoint (`CLAWCREW_LLM_BASE_URL`), and
persistent cloud if you switch `PERSISTENT_MODE`.

tenki is deliberately **not** a provider. it is what we use to test this repo on a
clean machine: `tenki sandbox create`, clone, `pip install -e ".[dev]"`, run the
suite. a build tool that proves a checkout works is different from a runtime that
agents' files live in, and the repo should not carry a cloud dependency just because
our ci uses one.
