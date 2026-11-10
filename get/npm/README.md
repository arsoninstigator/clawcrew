# get / npm

not built yet.

## what it will be

a small typed client for the http api, generated from the openapi document the server
already serves at `/openapi.json`, so the types cannot drift from the routes. the surface
it will cover, in the order it is worth writing:

- `crews`, `agents`, `tasks`, `channels`: the boring half, one method per route
- `ask()` and `approve()`: the front door, including polling a plan until it is approved
- `gates.watch()`: a stream of open approvals, since a link nobody sees is a run nobody
  finishes
- `brief()`: the night, as numbers, typed

## what to use today

`fetch` against the routes in `get/api`. every response is json with stable `code`
values on errors, and `/docs` lists the schema for each one. the python package in
`get/sdk` is the only client that exists, and it is not a client at all: it is the
server, imported.
