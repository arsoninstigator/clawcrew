# get / mcp

not built yet.

## what it will be

an mcp server so another agent, in another ide or on another machine, can treat a crew as
a set of tools instead of a set of endpoints. the tools map onto services that already
exist and are already tested:

- `ask(crew, text)` and `approve(ask_id)`: the front door
- `board(crew)` and `move(task_id, column)`: read the work, move the work
- `dispatch(crew, limit)`: start what is ready
- `brief(crew)`: what happened last night
- `gates(crew)` and `decide(gate_id)`: approvals, without a dashboard
- `channel_post(channel_id, text)`: say something where the crew reads it

## what it needs first

the permission story has to land before the tools do. an mcp client is an agent, so it
gets an agent identity, a profile and its own scopes, and it should be able to reach only
the crews and connectors it was granted. that work is `docs/backend.md` and
`manage/credential_broker`, not a wrapper around `curl`.

## what to use today

the http api (`get/api`) and the python package (`get/sdk`).
