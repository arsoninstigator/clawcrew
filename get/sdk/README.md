# get / sdk

the python package is the sdk. there is nothing to install over the wire: the same
services the api calls are importable, which is how the tests and the cli drive them.

```bash
pip install -e ".[dev]"
```

```python
from crittertown import build_app
from crittertown.config import Settings

app = build_app(Settings(database_path="crew.db"))

crew = app.crews.create_team("growth", mission="own the funnel")
wren = app.crews.create_agent(crew["id"], "wren", role="writer", skills=["writing"])

app.kanban.create_task(team_id=crew["id"], title="write the launch post")
app.runner.dispatch_ready(crew["id"])

# the newer surfaces hang off the same object graph
plan = app.asks.ask(crew["id"], "write the post and then send it")
brief = app.brief.build(crew["id"])
gate = app.gates.open(team_id=crew["id"], title="ship it?", prompt="")
app.triggers.install_pack(crew["id"], "overnight")
```

- `build_app()` is the only place services know about each other, so an embedded app
  behaves exactly like the server
- pass `Settings(...)` directly in tests and scripts, and `Settings.from_env()` when you
  want the env vars
- `model` can be injected (`build_app(settings, model=...)`) which is how the
  deterministic demo model runs the real orchestrator without calling an llm

the js/npm client is `get/npm`, and it is not built yet.
