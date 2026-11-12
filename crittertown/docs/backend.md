# backend

three boxes, and the rule that keeps them apart: **the app never holds a provider key, and
the database never holds one either.**

```
┌──────────────────┐        ┌───────────────────┐        ┌──────────────────┐
│ crittertown         │        │ postgres          │        │ agent-vault      │
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
what `CRITTERTOWN_DB` points at.

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
3. point the app at it: set `CRITTERTOWN_DB_URL` and install the extra

```bash
pip install 'crittertown[postgres]'
export CRITTERTOWN_DB_URL=postgresql://user:pw@host:5432/crittertown
```

`core.db.open_store` reads that one setting: empty means the sqlite file, anything else
means `core.store_postgres.PostgresStore`. nothing downstream changes, because it
offers the same four methods (`conn`, `tx`, `query`, `one`) and the wrapper translates
sqlite dialect at the driver's edge: `?` becomes `%s`, `insert or replace` becomes
`on conflict (<the schema's real primary key>) do update`, and `executescript` becomes
one statement at a time. that is why the three services that issue their own sql keep
working unchanged.

the schema is not a second file. `core/dialect.py` translates `core.db.SCHEMA` for both
the runtime and `make ddl`, `crittertown/tests/test_dialect.py` fails if
`docs/schema.postgres.sql` drifts from it (it had: the published file was 34 tables
against a 45 table schema, with an `autoincrement` postgres rejects), and
`crittertown/tests/test_store_postgres.py` drives the store through a fake driver, so a
`?` reaching psycopg or a transaction that stops being a transaction is a test failure
rather than a production surprise.

what is still sqlite-shaped, said plainly: `conn()` opens a connection per call and
`tx()` serializes writers inside one process. that is enough for a single node and for
a worker set that shares one postgres, and it is not a distributed lock. two processes
relying on `tx()` for mutual exclusion need a real advisory lock, which is not built.

## human auth, and the one credential that is a link

- **platform auth** (insforge or supabase) is for people: sessions, identity, storage
- **`CRITTERTOWN_API_TOKEN`** is for machines. one shared bearer token, checked in middleware,
  and `/health` stays open so a load balancer does not need it
- **gates** (`/g/{token}`) are the deliberate exception: the approval link *is* the
  credential. single use, bound to one decision, expiring, and refused after use, because
  the alternative is asking a shop owner to make an account to say "yes"

if you are putting a real ui in front of this, the ui gets a platform session and calls
crittertown with the token on the server side. crittertown does not model end users, it models
agents and one owner, and pretending otherwise would be two half-auth systems.

## per-agent credentials: agentvault, baked in

an agent with a `GITHUB_TOKEN` in its environment is one prompt injection away from
leaking it, and an agent that echoes its environment puts the key in a run row. so a key
is never handed to an agent. it is handed to a proxy, and the agent is given a route
through it.

[agentvault](https://github.com/Infisical/agent-vault), infisical's open-source agent vault, is that proxy: a single binary
that stores credentials, exposes a management api on `14321` and a MITM proxy on `14322`,
and attaches the real credential to outbound requests. run it on a **different machine**
from the agents, because that separation is the security claim:

```bash
docker run -d --name agent-vault \
  -p 14321:14321 -p 14322:14322 \
  -e CREDENTIAL_BROKER_MASTER_PASSWORD=... \
  -v agent-vault-data:/data agent-vault   # their published image, or build their repo
```

then, per agent, crittertown stores a **grant**, never a secret:

```bash
curl -X POST localhost:8000/agents/$AGENT/vault -H 'content-type: application/json' -d '{
  "label": "github",
  "token_env": "WREN_VAULT_TOKEN",
  "scopes": ["github.com", "api.github.com"],
  "expires_hours": 8
}'
```

- `token_env` is the **name** of the variable the brokered token arrives in. crittertown reads
  it from its own environment at spawn time and never stores it, logs it or returns it
- `scopes` are host suffixes. an unscoped grant defers to the proxy's own service rules; a
  scoped grant is also enforced by crittertown (`POST /vault/egress`), so widening the proxy
  cannot silently widen an agent
- `GET /agents/{id}/vault` shows the shape of what a sandbox would receive, with every
  value masked (`<set>` or `<missing>`), and `sandbox_env()` refuses to spawn an agent whose
  token is missing rather than starting it crippled
- revoking writes a row and an audit entry (`vault.revoked`). the grant history is the
  point: an audit trail that deletes itself is not one

### how the env reaches the sandbox

two paths, chosen by the provider, and the difference is a credential's safety rather than
a preference:

- **at creation**, when the provider's create request has an env field. `ensure()` takes
  `env` and the adapter forwards it; `delivers_env = True`. this is ephemeral sandboxes and
  the local dev provider. nothing records the values: the handle that lands in the
  `computers` row carries a ref, not an env
- **at boot**, when it does not. a computer create request has no env field
  (`delivers_env = False`, same for hosted whose sdk surface is theirs), so the computer is
  handed nothing and its harness calls `GET /agents/{agent_id}/sandbox-env` once at boot,
  authenticated with the platform token, and keeps the values in memory
  (`agents/runtime.py: pull_env`). a secret never sits in an exec command line or on the
  machine's disk, which is where the obvious implementation leaves it

per-agent env never reaches a row, a response or a log. `GET /agents/{id}/vault` shows the
shape with every value masked, `pull_env` returns names and a count, and
`crittertown/tests/test_sandbox_env.py` asserts the token is absent from the computers row
and from the response that created it.

### where the codes actually live

the broker distributes; it is not the vault of record. the two baked-in ingredients are
[agentvault](https://github.com/Infisical/agent-vault) for brokering and
[kernel](https://github.com/kernel/kernel-images) for browsers (a throwaway chromium in your
own container, per `crittertown/thirdparty.md`).

kernel's managed auth also offers a vault route that points at 1password, and that is
declined on purpose. the codes live upstream in **proton pass** or **bitwarden** (both open
source, both self-hostable), permissioned per agent, and the grant is issued **up the command
chain**: the agent asks, its coordinator in the org chart is the one who approves, and the
approval is a gate row with an audit trail rather than a shared master password.

honest status: the request-and-approve path exists (`gates`, `a2a`, the org chart), and what
is not built is the adapter that mints or reads a code from proton pass or bitwarden when a
gate is approved. until it is, the operator issues the brokered token by hand, which is the
same shape with one manual step.

## env

| var | meaning |
| --- | --- |
| `CRITTERTOWN_DB` | sqlite path (the runtime today) |
| `CREDENTIAL_BROKER_URL` | agent-vault management api, e.g. `http://vault:14321` |
| `CREDENTIAL_BROKER_PROXY_URL` | agent-vault MITM proxy, e.g. `http://vault:14322` |
| `CREDENTIAL_BROKER_TTL_HOURS` | default grant lifetime, 24 |
| `CRITTERTOWN_GATE_SECRET` | signs approval links; generated per db when unset |
| `CRITTERTOWN_PUBLIC_BASE_URL` | the host approval links are built against |
| `CRITTERTOWN_API_TOKEN` | shared bearer token for machines |
