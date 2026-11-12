# get / https

the server. one process serves http, one drains the queue, one ticks the clock, and none
of them need an api key to run the demo.

## local

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m core.cli demo      # seed, run a task, run a workflow, review a message
python -m core.cli serve     # http on :8000, openapi at /docs
```

## docker compose

```bash
cp .env.example .env
docker compose up --build
```

compose brings up the api, a worker on the same sqlite file in a volume, and a beat
container that runs `crittertown tick`. it is the smallest honest production-ish shape, not
a cluster.

## behind a proxy

put it behind your own tls terminator and set `CRITTERTOWN_API_TOKEN`. every route except
`/health` and the approval links (`/g/{token}`) then needs
`authorization: Bearer <token>` or `x-crittertown-token`. the approval link is deliberately
public: the link is the credential, it is single use, and it expires.

## a broker instead of the sqlite queue

the default is in process: `crittertown work` drains the queue that `crittertown serve` writes,
run it from cron or from a loop of your own, and nothing else is installed.

to move job ids between machines instead:

```bash
pip install "crittertown[schedule]"
CRITTERTOWN_BROKER=schedule SCHEDULE_BROKER_URL=redis://... crittertown serve
celery -A ingredients.schedule.jobs worker -Q crittertown
```

the sqlite queue stays the source of truth, so a broker changes who executes a job, never
what a job means, and a worker that dies mid job leaves a row to requeue.

## persistent computers

`crittertown get persistent --target local` wants a host with `/dev/kvm`, because the persistent
OSS runtime is microVMs. on a machine without nested virtualisation it says so instead of
pretending. see `crittertown/selfhost.md`.
