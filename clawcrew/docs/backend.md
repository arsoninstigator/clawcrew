# backend

three boxes, and the rule that keeps them apart: **the app never holds a provider key, and
the database never holds one either.**

```
┌──────────────────┐        ┌───────────────────┐        ┌──────────────────┐
│ clawcrew         │        │ postgres          │        │ agent-vault      │
│ api, worker,beat │───────▶│ insforge/supabase │        │ :14321 admin     │
│                  │        │ rows, auth, files │        │ :14322 proxy     │
└────────┬─────────┘        └───────────────────┘        └────────▲─────────┘
         │ spawns sandbox                                          │
         ▼                                                         │
┌──────────────────┐   HTTPS_PROXY + token, no keys   ─────────────┘
│ agent sandbox    │──────────────────────────────────────────────▶
└──────────────────┘
```

## the database

the store is sqlite today, and that is the runtime the 391 tests run against: one file,
wal mode, a serialized job claim. it is the right default for a single machine and it is
what `CLAWCREW_DB` points at.

`make ddl` writes `docs/schema.postgres.sql`, the same 34 tables in postgres dialect,
generated from the one schema definition rather than hand-maintained:

```bash
make ddl    # python scripts/export_ddl.py > docs/schema.postgres.sql
```

so hosting the rows on **insforge** (self-hosted, open source) or **supabase** is a store
adapter away, not a redesign:

1. bring up insforge or supabase, create the project, and open its sql editor
2. paste `docs/schema.postgres.sql` and run it. there are no extensions, no functions and
   no triggers to install: the audit chain and the queue claim are computed in python, so
   a postgres that only honours `create table` and `create index` is enough
3. point the app at it with a `Store` implementation for psycopg. the interface a
   postgres backend has to satisfy is four methods: `conn`, `tx`, `query`, `one`

what has to be fixed when that adapter lands, said plainly so nobody finds it in
production: `insert or replace into meta` becomes `on conflict (key) do update`, and
`executescript` becomes a list of statements. everything else in `clawcrew/db.py` is
already portable.

## human auth, and the one credential that is a link

- **platform auth** (insforge or supabase) is for people: sessions, identity, storage
- **`CLAWCREW_API_TOKEN`** is for machines. one shared bearer token, checked in middleware,
  and `/health` stays open so a load balancer does not need it
- **gates** (`/g/{token}`) are the deliberate exception: the approval link *is* the
  credential. single use, bound to one decision, expiring, and refused after use, because
  the alternative is asking a shop owner to make an account to say "yes"

if you are putting a real ui in front of this, the ui gets a platform session and calls
clawcrew with the token on the server side. clawcrew does not model end users, it models
agents and one owner, and pretending otherwise would be two half-auth systems.

## per-agent credentials: agent-vault by credential-broker

an agent with a `GITHUB_TOKEN` in its environment is one prompt injection away from
leaking it, and an agent that echoes its environment puts the key in a run row. so a key
is never handed to an agent. it is handed to a proxy, and the agent is given a route
through it.

[agent-vault](https://github.com/CredentialBroker/agent-vault) is that proxy: a single binary
that stores credentials, exposes a management api on `14321` and a MITM proxy on `14322`,
and attaches the real credential to outbound requests. run it on a **different machine**
from the agents, because that separation is the security claim:

```bash
docker run -d --name agent-vault \
  -p 14321:14321 -p 14322:14322 \
  -e CREDENTIAL_BROKER_MASTER_PASSWORD=... \
  -v agent-vault-data:/data credential-broker/agent-vault
```

then, per agent, clawcrew stores a **grant**, never a secret:

```bash
curl -X POST localhost:8000/agents/$AGENT/vault -H 'content-type: application/json' -d '{
  "label": "github",
  "token_env": "WREN_VAULT_TOKEN",
  "scopes": ["github.com", "api.github.com"],
  "expires_hours": 8
}'
```

- `token_env` is the **name** of the variable the brokered token arrives in. clawcrew reads
  it from its own environment at spawn time and never stores it, logs it or returns it
- `scopes` are host suffixes. an unscoped grant defers to the proxy's own service rules; a
  scoped grant is also enforced by clawcrew (`POST /vault/egress`), so widening the proxy
  cannot silently widen an agent
- `GET /agents/{id}/vault` shows the shape of what a sandbox would receive, with every
  value masked (`<set>` or `<missing>`), and `sandbox_env()` refuses to spawn an agent whose
  token is missing rather than starting it crippled
- revoking writes a row and an audit entry (`vault.revoked`). the grant history is the
  point: an audit trail that deletes itself is not one

### what is deliberately not wired

minting agent-vault tokens through its management api. the paths for that api are not
published in a form this repo can pin, and guessing endpoints is how a "provider support"
matrix rots. so the operator issues the token (vault ui, cli or their own automation) and
clawcrew distributes it per agent, with scopes, expiry and revocation. when the api is
stable enough to pin, the change is one method on `CredentialBroker`.

the second thing not wired: injecting `HTTPS_PROXY` into a sandbox at spawn. the env
contract is built and tested (`sandbox_env`), the provider interface takes no env argument
yet, so `clawcrew/docs/remaining.md` carries that as the next step. docker and k8s providers are
where it lands first, because they accept env at create time.

## env

| var | meaning |
| --- | --- |
| `CLAWCREW_DB` | sqlite path (the runtime today) |
| `CREDENTIAL_BROKER_URL` | agent-vault management api, e.g. `http://vault:14321` |
| `CREDENTIAL_BROKER_PROXY_URL` | agent-vault MITM proxy, e.g. `http://vault:14322` |
| `CREDENTIAL_BROKER_TTL_HOURS` | default grant lifetime, 24 |
| `CLAWCREW_GATE_SECRET` | signs approval links; generated per db when unset |
| `CLAWCREW_PUBLIC_BASE_URL` | the host approval links are built against |
| `CLAWCREW_API_TOKEN` | shared bearer token for machines |
