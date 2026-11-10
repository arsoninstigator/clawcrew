# get / https

the server. one process serves http, one drains the queue, one ticks the clock, and none
of them need an api key to run the demo.

## local

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m clawcrew.cli demo      # seed, run a task, run a workflow, review a message
python -m clawcrew.cli serve     # http on :8000, openapi at /docs
```

## docker compose

```bash
cp .env.example .env
docker compose up --build
```

compose brings up the api, a worker on the same sqlite file in a volume, and a beat
container that runs `clawcrew tick`. it is the smallest honest production-ish shape, not
a cluster.

## behind a proxy

put it behind your own tls terminator and set `CLAWCREW_API_TOKEN`. every route except
`/health` and the approval links (`/g/{token}`) then needs
`authorization: Bearer <token>` or `x-clawcrew-token`. the approval link is deliberately
public: the link is the credential, it is single use, and it expires.

## celery instead of the sqlite queue

```bash
CLAWCREW_BROKER=celery CELERY_BROKER_URL=redis://... clawcrew serve
celery -A clawcrew.jobs worker -Q clawcrew
```

the sqlite queue stays the source of truth, so moving to celery changes who executes a
job, never what a job means.

## persistent computers

`clawcrew get persistent --target local` wants a host with `/dev/kvm`, because the celesto
OSS runtime is microVMs. on a machine without nested virtualisation it says so instead of
pretending. see `SELF_HOSTING.md`.
