# third party

clawcrew is Apache-2.0. everything below was checked against the licence of the
named project before anything was reused. the distinction that matters: **code was
vendored only from permissive licences, and the two projects whose licences forbid
reuse are interoperated with at the file-format level only.**

## vendored code

these are copies of upstream code, committed under `clawcrew/vendor/`. each package
carries its upstream `LICENSE` and a `PROVENANCE.md` with the repository, the path and
the commit it came from.

| project | licence | what is vendored | how |
| --- | --- | --- | --- |
| [CelestoAI/celesto](https://github.com/CelestoAI/celesto) | Apache-2.0 | `src/_celesto_cloud_api`, the client generated from `openapi/cloud.json` | `clawcrew/vendor/celesto_cloud_api`. verbatim; only the parent package name changed, all inner imports were already relative. drives `sandbox/celesto.py`. |
| [e2b-dev/E2B](https://github.com/e2b-dev/E2B) | MIT | `packages/python-sdk/e2b/api/client`, the client generated from `spec/openapi.yml` | `clawcrew/vendor/e2b_api`. verbatim. drives the sandbox lifecycle in `sandbox/e2b.py`. |
| [a2aproject/a2a-python](https://github.com/a2aproject/a2a-python) | Apache-2.0 | `src/a2a/compat/v0_3/types.py` (generated from `specification/json/a2a.json` in a2aproject/A2A) and `src/a2a/_base.py` | `clawcrew/vendor/a2a_types`. one import rewrite, recorded in its `PROVENANCE.md`. used by `a2a_protocol.validate_wire`. |

## semantics ported, no code copied

| project | licence | what clawcrew took | how |
| --- | --- | --- | --- |
| [BloopAI/vibe-kanban](https://github.com/BloopAI/vibe-kanban) | Apache-2.0 | the per-project status model: a status is `{name, color, sort_order, hidden}` rather than a fixed enum, and `backlog`/`cancelled` are hidden by default | `KanbanService.column_meta` accepts string or object columns and keeps hidden columns out of the default board view. |
| [block/buzz](https://github.com/block/buzz) | Apache-2.0 | the idea that agent conversation is its own channel with its own lifecycle, not task comments; and, since comms, the bet that one signed event log holds people, agents and workflows alike | `a2a_messages` is a first class table with a status lifecycle. `clawcrew/comms` speaks their protocol (nip-01 events, nip-29 rooms) rather than their code, so a room can be mirrored to their relay. |
| [Mininglamp-OSS/octo-server](https://github.com/Mininglamp-OSS/octo-server) | Apache-2.0 | the shape of a workplace where agents are members with a session and per-room membership, and the lesson that an external im bridge is its own service | `comms_members` is per room with a member kind; `transports.py` keeps bridging out of the core. no code copied, and no client written against their long-poll bot api. |
| [a2aproject/A2A](https://github.com/a2aproject/A2A) | Apache-2.0 | the JSON-RPC binding, the `message/send` / `tasks/get` surface, the review-gate extension idea | `a2a_protocol.py` implements the binding over the vendored models. |

## format interop only (licence does not permit code reuse)

| project | licence | what clawcrew supports |
| --- | --- | --- |
| [n8n-io/n8n](https://github.com/n8n-io/n8n) | Sustainable Use License (source available, not OSI) | import of exported workflow json (`nodes` + `connections`) into a clawcrew dag, `clawcrew/importers.py:from_n8n`. implemented from the format; no n8n source in this repo. |
| [langgenius/dify](https://github.com/langgenius/dify) | Dify Open Source License (Apache-2.0 plus additional conditions) | import of workflow exports (`workflow.graph.nodes` + `edges`), `from_dify`. same rule. |

why not copy: the sustainable use licence forbids using the code to provide a
competing hosted service, and dify's adds multi-tenant and branding conditions.
neither can be relicensed into an Apache-2.0 backend. importing their *format* is
interoperability, which is what users of an open tool actually need.

## checked, not used

| project | licence | why nothing was taken |
| --- | --- | --- |
| [langflow-ai/langflow](https://github.com/langflow-ai/langflow) | MIT | a dag editor in python. nothing here is a canvas yet; when the canvas is built, this is the first place to port from, and MIT allows it. |
| `orchestrator.inc`, `ando.so`, `zop.dev`, `celery.dev` | unknown | could not resolve a public repository or licence for these names, so nothing was taken and no claim is made about them. celery itself is BSD-3-Clause and is used as an optional broker, not vendored. |

## specifications implemented from their text

not code reuse, but the reason the wire is interoperable, so it is recorded here:

| spec | what clawcrew implements | where |
| --- | --- | --- |
| [NIP-01](https://github.com/nostr-protocol/nips/blob/master/01.md) | the event envelope: canonical serialization, `id` as sha256 of it, filters (`ids`, `authors`, `kinds`, `since`, `until`, `#tag`) | `clawcrew/comms/events.py` |
| [NIP-29](https://github.com/nostr-protocol/nips/blob/master/29.md) | relay-based groups: kind 9 with an `h` tag, 9000/9001 roster changes, 39000 group metadata (reserved) | `clawcrew/comms/events.py` |
| [BIP-340](https://github.com/bitcoin/bips/blob/master/bip-0340.mediawiki) | schnorr signatures over secp256k1, so a signature is one any relay accepts | `clawcrew/comms/secp256k1.py` |
| [BIP-173](https://github.com/bitcoin/bips/blob/master/bip-0173.mediawiki) | bech32, for the `npub1...` and `nsec1...` spellings | `clawcrew/comms/bech32.py` |

## obligations we carry

- Apache-2.0 section 4: this `NOTICE` travels with distributions, and every vendored
  file tree keeps its upstream licence and a provenance record naming the commit.
- MIT (e2b): the copyright notice and permission notice stay with the vendored copy,
  which is why `clawcrew/vendor/e2b_api/LICENSE` is the upstream file, unedited.
- no project listed above is claimed as endorsed by clawcrew, and clawcrew is not
  endorsed by any of them.
