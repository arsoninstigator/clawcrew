# configure

everything is an env var. there is no config file, nothing is discovered, and no
setting has a default that depends on the machine it is running on. copy `.env.example`,
change what you need, and the same image runs anywhere.

## core

| var | default | what it does |
| --- | --- | --- |
| `CRITTERTOWN_DB` | `crittertown.db` | sqlite file. the queue, the boards, the audit chain and the credential grants all live here |
| `CRITTERTOWN_API_TOKEN` | unset | shared bearer token. unset means open, which is fine on localhost and wrong on a network |
| `CRITTERTOWN_PUBLIC_BASE_URL` | unset | the host approval links are built against. without it a link points at `localhost` |
| `CRITTERTOWN_GATE_SECRET` | generated per db | signs approval links. set it once and keep it, or old links die on restart |
| `CRITTERTOWN_GATE_TTL_HOURS` | `168` | how long an approval link stays valid |
| `CRITTERTOWN_BRIEF_START` `CRITTERTOWN_BRIEF_END` | `18` `6` | the window the morning brief reads |
| `CRITTERTOWN_REVIEW_MODE` | `reviewed` | default review mode for new crews |
| `CRITTERTOWN_MAX_PARALLEL` | `4` | concurrent dispatches per `dispatch_ready` |
| `CRITTERTOWN_SANDBOX_TIMEOUT` | `900` | hard ceiling for one command, in seconds |

## the model

| var | default | what it does |
| --- | --- | --- |
| `CRITTERTOWN_LLM_BASE_URL` | unset | any openai-compatible endpoint. unset switches runs onto the deterministic model |
| `CRITTERTOWN_LLM_API_KEY` | unset | key for that endpoint |
| `CRITTERTOWN_LLM_MODEL` | `gpt-4.1-mini` | model name to send |
| `CRITTERTOWN_BROKER` | `inproc` | `inproc` for one process, `schedule` for a cluster |
| `SCHEDULE_BROKER_URL` | unset | redis or rabbitmq when the broker is schedule |
| `SCHEDULE_QUEUE` | `crittertown` | the queue name a worker drains |

## compute

three machines, three verbs: persistent is a computer that keeps its disk, ephemeral is a
sandbox that lasts one task, hosted is somebody else's runtime when you opt in.

| var | default | what it does |
| --- | --- | --- |
| `CRITTERTOWN_ALLOW_LOCAL_FALLBACK` | `true` | `false` turns a missing provider key into an error instead of a local shell |
| `PERSISTENT_MODE` | `local` | `local` runs the persistent runtime on this machine, `cloud` is their hosted api |
| `PERSISTENT_BIN` | `persistent` | the binary `local` mode drives |
| `PERSISTENT_API_KEY` | unset | the key for `cloud` mode |
| `PERSISTENT_BASE_URL` | their api | override only for your own deployment |
| `PERSISTENT_IMAGE` | `crittertown/persistent:latest` | default image for a new computer |
| `EPHEMERAL_BASE_URL` | `http://localhost:3000` | your instance's api |
| `EPHEMERAL_API_KEY` | unset | the key your deployment generates |
| `EPHEMERAL_TEMPLATE` | `base` | default template for a new sandbox |
| `EPHEMERAL_EXEC_MODULE` | unset | the import name of the upstream sdk, for a fork that renamed it |
| `HOSTED_API_KEY` | unset | opt in. their runtime, their sdk, no byoc |
| `HOSTED_BASE_URL` `HOSTED_TEMPLATE` | unset | same, for choosing what it boots |
| `DOCKER_HOST` `SSH_HOST` `K8S_CONTEXT` | unset | the byoc targets, when you have your own iron |

the sandbox lifecycle runs on the vendored client in `vendor/`. commands inside an
ephemeral sandbox do not: they go over envd, which only the upstream python sdk
implements, so `pip install "crittertown[ephemeral]"` is what makes exec work. without it
the adapter refuses with that hint instead of inventing a route.

## browser

no account and no key: a driver over cdp, and a chromium you run yourself.

| var | default | what it does |
| --- | --- | --- |
| `CRITTERTOWN_BROWSER_PROVIDER` | `local-browser` | `local-browser`, `container-browser`, or `none` to give an agent no browser |
| `CRITTERTOWN_BROWSER_CDP` | unset | attach to a browser you already started |
| `CRITTERTOWN_CONTAINER_BROWSER_IMAGE` | unset | the chromium image container-browser starts |
| `CRITTERTOWN_BROWSER_PROFILE_ROOT` | unset | where profiles and downloads live |

## the board

`manage` is the project management panel. the platform drives it, and each agent gets its
own key so its work is attributed to it.

```sh
CRITTERTOWN_MANAGE_BASE_URL=http://localhost:4000   # the api origin of your instance
CRITTERTOWN_MANAGE_API_KEY=                         # an external agent's key, from its page
```

create the agent in the instance, give it a role and a project, copy its key. the bridge is
the whole loop: claim one run, heartbeat it, run the crew's turn, post the result. with no
key the board reports itself as not configured and every board call says so instead of
guessing.

## workflows

a flow is a service, not a workflow node: an agent calls it by id when it wants one.

```sh
CRITTERTOWN_WORKFLOWS_BASE_URL=http://localhost:7860  # a workflows instance you run
CRITTERTOWN_WORKFLOWS_API_KEY=
```

## toolbox

engines that decide their own order, so an agent posts to them rather than the other way
round. both are called on your own instance; neither is copied.

| var | default | what it does |
| --- | --- | --- |
| `CRITTERTOWN_N8N_BASE_URL` `CRITTERTOWN_N8N_API_KEY` | unset | a node can post to your n8n, and its exports import |
| `CRITTERTOWN_DIFY_BASE_URL` `CRITTERTOWN_DIFY_API_KEY` `CRITTERTOWN_DIFY_USER` | unset | a node can run your dify workflow, and its exports import |
| `CRITTERTOWN_FIRECRAWL_API_KEY` `CRITTERTOWN_FIRECRAWL_BASE_URL` | unset | the worked example: an agent scrapes, crawls or maps a site through your key |

## credentials

| var | default | what it does |
| --- | --- | --- |
| `CREDENTIAL_BROKER_URL` | unset | the management api, e.g. `http://vault:14321` |
| `CREDENTIAL_BROKER_PROXY_URL` | unset | the mitm proxy, e.g. `http://vault:14322` |
| `CREDENTIAL_BROKER_TTL_HOURS` | `24` | default lifetime of a grant |

an agent never holds a key. it gets a proxy url, the name of a variable its brokered token
arrives in, and a host scope. without these three set, agents use plain environment
variables and the credential routes say so instead of pretending.

## comms

| var | default | what it does |
| --- | --- | --- |
| `CRITTERTOWN_COMMS_CUSTODY` | `local` | `local` mints and holds each agent's key here; `external` stores public halves only |
| `CRITTERTOWN_COMMS_HUMANS` | `false` | agents are the only first class members of a room unless this is on |
| `CRITTERTOWN_COMMS_RELAY` | unset | a relay every new room is bridged to; empty means local rooms only |
| `CRITTERTOWN_COMMS_RELAY_TRANSPORT` | `nostr` | the transport for that bridge |
| `CRITTERTOWN_COMMS_LIMIT` | `200` | page size when reading a room |

## not configuration

licences and provenance for the vendored clients are in `crittertown/thirdparty.md`, the
schema and deployment story is in `crittertown/docs/backend.md`, and the words are in
`crittertown/glossary.md`.
