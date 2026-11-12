# credits

crittertown is Apache-2.0. this is the one file where the upstream projects are named by
their own names.

the rule everywhere else in the repo: a component we run inside the platform is an
**ingredient**, and it is called by what it does (`persistent`, `ephemeral`, `browser`,
`schedule`, `durableobjects`, `workflows`, `manage`, `kanban`, `credential broker`).
something we call over a network is **toolbox**. so a path, an env var or a log line
says what the thing is for, and this file says which project it is.

## baked in (ingredients), code vendored

these are copies of upstream code under `vendor/`, each with its upstream `LICENSE` and a
`PROVENANCE.md` naming repository, path and commit.

| in house | upstream | licence | vendored | drives |
| --- | --- | --- | --- | --- |
| persistent | [PersistentAI/celesto](https://github.com/PersistentAI/celesto) | Apache-2.0 | `src/_celesto_cloud_api`, the client generated from `openapi/cloud.json` | `ingredients/machines/persistent/client.py` |
| ephemeral | [e2b-dev/E2B](https://github.com/e2b-dev/E2B) | MIT | `packages/python-sdk/e2b/api/client`, generated from `spec/openapi.yml` | `ingredients/machines/ephemeral/client.py` |
| (wire types) | [a2aproject/a2a-python](https://github.com/a2aproject/a2a-python) | Apache-2.0 | `src/a2a/compat/v0_3/types.py` and `src/a2a/_base.py` | `comms/protocol.py` |

## baked in (ingredients), no code copied

| in house | upstream | licence | what crittertown uses of it | boundary |
| --- | --- | --- | --- | --- |
| browser | [kernel.sh's kernel-images](https://github.com/kernel/kernel-images) + [browser-use](https://github.com/browser-use/browser-use) | Apache-2.0 / MIT | a throwaway chromium built locally in docker (`images/chromium-headful`), driven by browser-use attached to a page over cdp | your own container and your own driver; no hosted control plane and no api key |
| schedule | [celery/celery](https://github.com/celery/celery) | BSD-3-Clause | an optional broker behind the same `Scheduler` interface as the in-process one, off unless `CRITTERTOWN_BROKER=schedule` | a broker url, nothing imported. the distribution name carries it (`crittertown[schedule]`), and `ingredients/schedule/jobs.py` is the one module that imports it, because a python import cannot be aliased |
| durableobjects | [TerseAI/durable-actors](https://github.com/TerseAI/durable-actors) | MIT | durable coordination: a controller on `:7100`, postgres + a bucket, invoke-only key | your deployment; the client is ours |
| workflows | [langflow-ai/langflow](https://github.com/langflow-ai/langflow) | MIT | a flow, by id, over its api: rag and chain steps an agent calls when it wants one | your instance, your key, never vendored |
| manage | [croffasia/itsaplan](https://github.com/croffasia/itsaplan) | AGPL-3.0 | the board: projects, issues, agents as members, and the runner contract (claim, heartbeat, events, result) | a socket, never an import. the strong licence is exactly why |
| kanban | [BloopAI/vibe-kanban](https://github.com/BloopAI/vibe-kanban) | Apache-2.0 | the per-project status model: a status is `{name, color, sort_order, hidden}`, and `backlog`/`cancelled` hide by default | ported semantics, no code |
| credential broker | [Infisical's agent-vault](https://github.com/Infisical/agent-vault) | MIT | the shape of a grant: proxy url, a token env name, host scopes, expiry | we speak to it; its binary is yours to run |

## toolbox (called, not baked)

| project | licence | how |
| --- | --- | --- |
| [n8n-io/n8n](https://github.com/n8n-io/n8n) | Sustainable Use License (source available, not OSI) | export format imported in `agents/importers.py:from_n8n`, and a client in `toolbox/workflows/n8n.py` for your own instance |
| [langgenius/dify](https://github.com/langgenius/dify) | Dify Open Source License (Apache-2.0 plus conditions) | `from_dify` for exports, `toolbox/workflows/dify.py` for your instance |
| [firecrawl](https://github.com/firecrawl/firecrawl) | AGPL-3.0 | the worked example of a toolbox entry: `toolbox/firecrawl/` calls scrape, crawl and map on your key. a tool an agent calls, so it lives outside the platform, and its licence is the reason it is never copied |

why not copy n8n or dify: the sustainable use licence forbids using the code to provide a
competing hosted service, and dify's adds multi-tenant and branding conditions. neither can
be relicensed into an Apache-2.0 backend. importing their format, or calling their daemon, is
interoperability, which is what users of an open tool actually need.

## read, not used

| project | licence | what it changed here |
| --- | --- | --- |
| [paperclipai/paperclip](https://github.com/paperclipai/paperclip) | see their repo | the lifecycle an hmas needs: heartbeats as the only way an agent spends time, a wakeup that names its reason, atomic checkout where losing a race is a normal answer, and a delegation chain that traces an issue back to a goal. no code taken |
| [block/buzz](https://github.com/block/buzz) | Apache-2.0 | the idea that agent conversation is its own channel with its own lifecycle, not task comments |
| [a2aproject/A2A](https://github.com/a2aproject/A2A) | Apache-2.0 | the JSON-RPC binding and the review-gate extension |

## obligations we carry

- Apache-2.0 section 4: `NOTICE` travels with distributions, and every vendored tree keeps
  its upstream licence and a provenance record naming the commit.
- MIT (ephemeral): the copyright and permission notice stay with the vendored copy, which is
  why `vendor/ephemeral_cloud_api/LICENSE` is the upstream file, unedited.
- no project listed here is claimed to endorse crittertown, and crittertown endorses none of them.
