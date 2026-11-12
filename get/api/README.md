# get / api

the http surface. everything a frontend needs is a route, and the routes hold no logic:
they validate, call one service method, and return.

## first call

```bash
export CRITTERTOWN_API_TOKEN=dev        # optional locally, required behind a proxy
curl -s localhost:8000/health
curl -s -X POST localhost:8000/crews -H 'content-type: application/json' \
  -d '{"name":"growth","mission":"own the funnel"}'
```

## the four that make it a platform

```bash
# an outcome in, a reviewed plan out
curl -s -X POST localhost:8000/crews/$CREW/ask -H 'content-type: application/json' \
  -d '{"text":"write the launch post and then schedule it for monday"}'

# the night, as numbers
curl -s -X POST localhost:8000/crews/$CREW/brief

# what needs a human, as links
curl -s "localhost:8000/gates?team_id=$CREW"

# what actually fired last night
curl -s "localhost:8000/crews/$CREW/triggers"
```

## auth

`authorization: Bearer <CRITTERTOWN_API_TOKEN>` or `x-crittertown-token`. `/health` and
`/g/{token}` stay open on purpose: a health check must not need a secret, and an approval
link is the secret.

## the rest

`/docs` is the live openapi ui, and `README.md` in the repo root carries the grouped route
table. errors are one ladder with stable codes, so a client can branch on `code` instead
of matching prose.
