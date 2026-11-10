"""command line.

    clawcrew seed            create the demo crew and print what it made
    clawcrew inspect         counts + review queue
    clawcrew dispatch <id>   run one task now
    clawcrew tick            enqueue anything due
    clawcrew work [--limit]  drain the queue
    clawcrew serve [--port]  run the http api
    clawcrew demo            seed + dispatch + review, end to end, no network
    clawcrew get persistent  plan (and with --apply, create) a computer that stays
    clawcrew get disposable  plan (and with --apply, create) one that is thrown away
    clawcrew connect <cloud> one step byoc: detect what this machine already has
"""

from __future__ import annotations

import argparse
import json
import os

from clawcrew.app import build_app
from clawcrew.config import Settings
from clawcrew.errors import ClawCrewError


def _app(args: argparse.Namespace):
    settings = Settings.from_env()
    if getattr(args, "db", None):
        settings.database_path = args.db
    from clawcrew.create.agents.runtime import DeterministicModel

    model = DeterministicModel() if getattr(args, "dry_run", False) else None
    return build_app(settings, model=model)


def cmd_seed(args: argparse.Namespace) -> int:
    app = _app(args)
    crew = app.crews.create_team(
        "growth crew",
        mission="own the top of funnel: research, content, and launch sequencing",
        review_mode="reviewed",
    )
    agents = [
        ("scout", "market research", ["research", "browser"], "you find primary sources and never guess a number."),
        ("writer", "long form content", ["writing"], "you write plainly and cite the crew's own research."),
        ("operator", "deployment and ops", ["bash", "deploy"], "you ship, and you snapshot before anything destructive."),
    ]
    for name, role, skills, prompt in agents:
        app.crews.add_agent(crew["id"], name, role=role, skills=skills, system_prompt=prompt)
    app.crews.put_context(crew["id"], "icp", {"segment": "solo founders", "arr_band": "<100k"})
    app.crews.put_context(crew["id"], "voice", {"tone": "plain", "banned_words": ["revolutionary", "synergy"]})

    workflow = app.workflows.save(
        crew["id"],
        "weekly digest",
        {
            "nodes": [
                {"id": "brief", "type": "set_context", "key": "brief", "value": "weekly digest for ${trigger.week}"},
                {"id": "research", "type": "agent_task", "agent": "scout",
                 "title": "gather ${trigger.week} signals", "instruction": "collect five primary sources",
                 "depends_on": ["brief"]},
                {"id": "write", "type": "agent_task", "agent": "writer",
                 "title": "draft the digest", "instruction": "write from the research only",
                 "depends_on": ["research"]},
                {"id": "approve", "type": "human_gate", "title": "approve the digest", "depends_on": ["write"]},
                {"id": "ship", "type": "shell", "agent": "operator",
                 "command": "echo shipped ${brief}", "depends_on": ["approve"]},
            ]
        },
    )
    app.scheduler.create(
        crew["id"], "monday digest", "cron", "0 8 * * 1",
        {"team_id": crew["id"], "title": "weekly digest run", "required_skills": ["research"]},
    )
    print(json.dumps({
        "crew": crew["id"],
        "agents": [a["id"] for a in app.crews.list_agents(crew["id"])],
        "workflow": workflow["id"],
        "task_example": "clawcrew dispatch <task_id>",
    }, indent=2))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    app = _app(args)
    print(json.dumps({"counts": app.counts(), "providers": app.registry.status()}, indent=2))
    for crew in app.crews.list_teams():
        print(f"\n{crew['name']} ({crew['id']}) review={crew['review_mode']}")
        print("  columns:", app.kanban.board(crew["id"])["counts"])
        print("  a2a:", app.a2a.stats(crew["id"]))
        print("  context keys:", sorted(app.crews.context(crew["id"])["values"]))
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    app = _app(args)
    outcome = app.runner.dispatch(args.task_id, max_turns=args.max_turns)
    print(json.dumps({
        "run": outcome["run_id"],
        "column": outcome["task"]["column"],
        "agent": outcome["agent"]["name"],
        "provider": outcome["provider"],
        "steps": len(outcome["steps"]),
    }, indent=2))
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    app = _app(args)
    print(json.dumps({"enqueued": app.scheduler.tick()}, indent=2))
    return 0


def cmd_work(args: argparse.Namespace) -> int:
    app = _app(args)
    print(json.dumps({"results": app.worker.process(args.limit)}, indent=2, default=str))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    app = _app(args)
    crew = app.crews.create_team("demo crew", mission="prove the loop works")
    scout = app.crews.add_agent(crew["id"], "scout", role="research", skills=["research"])
    app.crews.add_agent(crew["id"], "writer", role="writing", skills=["writing"])
    task = app.kanban.create_task(crew["id"], "collect seed signals", column="ready", required_skills=["research"])
    outcome = app.runner.dispatch(task["id"])

    workflow = app.workflows.save(crew["id"], "demo flow", {"nodes": [
        {"id": "note", "type": "set_context", "key": "demo", "value": "ran"},
        {"id": "gate", "type": "human_gate", "title": "approve demo", "depends_on": ["note"]},
        {"id": "after", "type": "shell", "agent": "scout", "command": "echo after the gate", "depends_on": ["gate"]},
    ]})
    flow_run = app.workflows.run(workflow["id"])

    messages = app.a2a.review_queue(crew["id"])
    approved = app.a2a.review(messages[0]["id"], "approved", reviewer="cli") if messages else None

    print(json.dumps({
        "task_column_after_run": outcome["task"]["column"],
        "run_steps": len(outcome["steps"]),
        "workflow_status": flow_run["status"],
        "review_queue": len(messages),
        "message_after_review": approved["status"] if approved else None,
        "context": sorted(app.crews.context(crew["id"])["values"]),
        "crew": crew["id"],
        "agent": scout["id"],
    }, indent=2))
    return 0


def _provisioner(args: argparse.Namespace):
    from clawcrew.create.sandbox.provision import Provisioner

    return Provisioner(_settings(args))


def _settings(args: argparse.Namespace) -> Settings:
    settings = Settings.from_env()
    if getattr(args, "db", None):
        settings.database_path = args.db
    return settings


def cmd_get(args: argparse.Namespace) -> int:
    """plan by default. creating a machine, and spending money, needs --apply."""
    provisioner = _provisioner(args)
    spec = {"name": args.name, "region": args.region} if args.region else {"name": args.name}
    if args.host:
        spec["ssh_host"] = args.host
    if args.image:
        spec["image"] = args.image
    plan = provisioner.plan(args.kind, args.target, spec)
    outcome = provisioner.apply(plan, dry_run=not args.apply)
    print(json.dumps(outcome, indent=2))
    if not args.apply:
        print(f"\nnothing was created. add --apply when the plan looks right "
              f"({len(plan.steps)} step(s)).", file=os.sys.stderr)
    return 0 if outcome.get("ok", True) else 1


def cmd_destroy(args: argparse.Namespace) -> int:
    outcome = _provisioner(args).destroy(args.kind, args.name, args.target)
    print(json.dumps(outcome, indent=2))
    return 0 if outcome["destroyed"] else 1


def cmd_connect(args: argparse.Namespace) -> int:
    outcome = _provisioner(args).connect(args.provider)
    print(json.dumps(outcome, indent=2))
    return 0 if outcome["ready"] else 1


def cmd_targets(args: argparse.Namespace) -> int:
    print(json.dumps({"targets": _provisioner(args).targets()}, indent=2))
    return 0


def cmd_coordinator(args: argparse.Namespace) -> int:
    """durable coordination, self hosted. prints the helm plan unless --apply."""
    from clawcrew.create.coordination.durable_actors import plan_runtime
    from clawcrew.create.plans import run_plan
    from clawcrew.create.sandbox.provision import Provisioner

    provisioner = Provisioner(_settings(args))
    try:
        plan = plan_runtime(
            _settings(args), args.public_url, args.bucket, args.gsa, args.zone,
            namespace=args.namespace, version=args.version, ingress=not args.no_ingress)
    except ClawCrewError as exc:
        print(json.dumps(exc.to_dict(), indent=2))
        return 1
    outcome = run_plan(plan, provisioner._runner, dry_run=not args.apply)
    outcome["contract"] = __import__(
        "clawcrew.create.coordination.durable_actors", fromlist=["actor_contract"]
    ).actor_contract()
    print(json.dumps(outcome, indent=2))
    return 0 if outcome.get("ok", True) else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    os.environ.setdefault("CLAWCREW_DB", args.db or "clawcrew.db")
    uvicorn.run("clawcrew.api:create_api", factory=True, host=args.host, port=args.port, reload=args.reload)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clawcrew", description="agent crews with computers and workflows")
    parser.add_argument("--db", help="sqlite path (default $CLAWCREW_DB or clawcrew.db)")
    parser.add_argument("--dry-run", action="store_true", help="use the deterministic model, no llm calls")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="create the demo crew").set_defaults(func=cmd_seed)
    sub.add_parser("inspect", help="show counts, boards, review queues").set_defaults(func=cmd_inspect)
    dispatch = sub.add_parser("dispatch", help="run one task")
    dispatch.add_argument("task_id")
    dispatch.add_argument("--max-turns", type=int, default=6)
    dispatch.set_defaults(func=cmd_dispatch)
    sub.add_parser("tick", help="enqueue due schedules").set_defaults(func=cmd_tick)
    work = sub.add_parser("work", help="drain the job queue")
    work.add_argument("--limit", type=int, default=10)
    work.set_defaults(func=cmd_work)
    sub.add_parser("demo", help="seed, run, and review end to end").set_defaults(func=cmd_demo)
    serve = sub.add_parser("serve", help="run the http api")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    get = sub.add_parser("get", help="plan a persistent or disposable computer")
    get.add_argument("kind", choices=["persistent", "disposable"])
    get.add_argument("--target", default="",
                     help="local | celesto | e2b | docker | ssh | k8s | aws | gcp | azure | oracle")
    get.add_argument("--name", default="clawcrew-1")
    get.add_argument("--host", default="", help="user@host, for the ssh target")
    get.add_argument("--region", default="")
    get.add_argument("--image", default="")
    get.add_argument("--apply", action="store_true",
                     help="actually create it; without this only the plan is printed")
    get.set_defaults(func=cmd_get)
    kill = sub.add_parser("destroy", help="remove a computer by name")
    kill.add_argument("kind", choices=["persistent", "disposable"])
    kill.add_argument("name")
    kill.add_argument("--target", default="")
    kill.set_defaults(func=cmd_destroy)
    connect = sub.add_parser("connect", help="detect credentials for a byoc target")
    connect.add_argument("provider")
    connect.set_defaults(func=cmd_connect)
    sub.add_parser("targets", help="what this machine can provision").set_defaults(func=cmd_targets)
    coordinator = sub.add_parser(
        "coordinator", help="plan a self hosted durable actor runtime for leases and streams")
    coordinator.add_argument("--public-url", default="")
    coordinator.add_argument("--bucket", default="")
    coordinator.add_argument("--gsa", default="")
    coordinator.add_argument("--zone", default="")
    coordinator.add_argument("--namespace", default="actors")
    coordinator.add_argument("--version", default="")
    coordinator.add_argument("--no-ingress", action="store_true")
    coordinator.add_argument("--apply", action="store_true")
    coordinator.set_defaults(func=cmd_coordinator)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ClawCrewError as exc:
        print(json.dumps(exc.to_dict(), indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
