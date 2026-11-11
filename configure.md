# configure

everything is an env var. there is no config file, nothing is discovered, and no
setting has a default that depends on the machine it is running on. copy `.env.example`,
change what you need, and the same image runs anywhere.

## core

| var | default | what it does |
| --- | --- | --- |
| `CLAWCREW_DB` | `./demo_data/clawcrew.db` | sqlite file. the queue, the boards, the audit chain and the vault grants all live here |
| `CLAWCREW_API_TOKEN` | unset | shared bearer token. unset means open, which is fine on localhost and wrong on a network |
| `CLAWCREW_PUBLIC_BASE_URL` | unset | the host approval links are built against. without it a link points at `localhost` |
| `CLAWCREW_GATE_SECRET` | generated per db | signs approval links. set it once and keep it, or old links die on restart |
| `CLAWCREW_GATE_TTL_HOURS` | `168` | how long an approval link stays valid |
| `CLAWCREW_REVIEW_MODE` | `reviewed` | default review mode for new crews |
| `CLAWCREW_MAX_PARALLEL` | `2` | concurrent dispatches per `dispatch_ready` |

## the model

| var | default | what it does |
| --- | --- | --- |
| `CLAWCREW_LLM_BASE_URL` | unset | any openai-compatible endpoint. unset switches runs onto the deterministic model |
| `CLAWCREW_LLM_API_KEY` | unset | key for that endpoint |
| `CLAWCREW_LLM_MODEL` | `gpt-4o-mini` | model name to send |
| `CLAWCREW_BROKER` | `inproc` | `inproc` for one process, `celery` for a cluster |
| `CELERY_BROKER_URL` | unset | redis or rabbitmq when the broker is celery |
| `CELERY_QUEUE` | `clawcrew` | the queue name a worker drains |

## compute

| var | default | what it does |
| --- | --- | --- |
| `CLAWCREW_ALLOW_LOCAL_FALLBACK` | `true` | `false` turns a missing provider key into an error instead of a local sandbox |
| `CELESTO_API_KEY` | unset | hosted microVMs. unset uses the celesto OSS runtime locally |
| `CELESTO_MODE` | `local` | `local` or `cloud` |
| `E2B_API_KEY` | unset | ephemeral sandboxes |
| `SUPERSERVE_API_KEY` | unset | opt in. their runtime, their sdk, no byoc |
| `CLAWCREW_SANDBOX_TIMEOUT_SECONDS` | `600` | hard ceiling for one command |
| `DOCKER_HOST` `SSH_HOST` `K8S_CONTEXT` | unset | the byoc targets, when you have your own iron |

## self-hosted tools

| var | default | what it does |
| --- | --- | --- |
| `CLAWCREW_N8N_BASE_URL` `CLAWCREW_N8N_API_KEY` | unset | a workflow node can post to your n8n |
| `CLAWCREW_DIFY_BASE_URL` `CLAWCREW_DIFY_API_KEY` `CLAWCREW_DIFY_USER` | unset | a workflow node can run your dify workflow |
| `CLAWCREW_LANGFLOW_BASE_URL` `CLAWCREW_LANGFLOW_API_KEY` | unset | a workflow node can run a langflow flow |
| `CLAWCREW_SIM_BASE_URL` `CLAWCREW_SIM_API_KEY` | unset | a workflow node can run a sim workflow |
| `CLAWCREW_KERNEL_IMAGES_IMAGE` | unset | a chromium you built from kernel-images, for browser work |
| `CLAWCREW_KERNEL_CDP` | unset | attach to a browser you already started |

## credentials

| var | default | what it does |
| --- | --- | --- |
| `AGENT_VAULT_URL` | unset | agent-vault management api, e.g. `http://vault:14321` |
| `AGENT_VAULT_PROXY_URL` | unset | agent-vault MITM proxy, e.g. `http://vault:14322` |
| `AGENT_VAULT_TTL_HOURS` | `24` | default lifetime of a grant |

an agent never holds a key. it gets a proxy url, the name of a variable its brokered token
arrives in, and a host scope. without these three set, agents use plain environment
variables and the vault routes say so instead of pretending.

## not configuration

licences and provenance for the vendored clients are in `clawcrew/thirdparty.md`, the
schema and deployment story is in `clawcrew/docs/backend.md`, and the words are in
`clawcrew/glossary.md`.

### the board

```sh
CLAWCREW_ITSAPLAN_BASE_URL=http://localhost:4000   # the api origin of your itsaplan instance
CLAWCREW_ITSAPLAN_API_KEY=                         # an external agent's key, from its page in the instance
```

create the agent in the instance, give it a role and a project, copy its key.
`ItsaplanBridge.poll()` is the whole loop: claim one run, heartbeat it, run the crew's turn,
post the result. with no key the board is reported as not
configured and every board call says so instead of guessing. enable the mcp server per
project there if you also want its tools inside an agent's own cli.

### flows

```sh
CLAWCREW_LANGFLOW_BASE_URL=http://localhost:7860  # a langflow instance you run
CLAWCREW_LANGFLOW_API_KEY=
```

a flow is a service, not a workflow node: an agent calls it when it wants one.
