"""command line.

    crittertown seed            create the demo crew and print what it made
    crittertown inspect         counts + review queue
    crittertown dispatch <id>   run one task now
    crittertown tick            enqueue anything due
    crittertown work [--limit]  drain the queue
    crittertown serve [--port]  run the http api
    crittertown demo            seed + dispatch + review, end to end, no network
    crittertown comms <verb>    keys, rooms, bridges and sync for the signed log
"""

from __future__ import annotations

import argparse
import json
import os

from core.app import build_app
from core.config import Settings
from core.errors import CritterTownError


def _app(args: argparse.Namespace):
    settings = Settings.from_env()
    if getattr(args, "db", None):
        settings.database_path = args.db
    from agents.runtime import DeterministicModel

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
        "task_example": "crittertown dispatch <task_id>",
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


def cmd_comms(args: argparse.Namespace) -> int:
    """one entry point for the room log: who has keys, what a room says, what a
    bridge would do. nothing here is destructive and nothing prints a secret."""
    app = _app(args)
    verb = args.verb
    if verb == "status":
        rooms = app.store.query("select id, name from channels order by created_date")
        print(json.dumps({
            "custody": app.settings.comms_custody,
            "humans_enabled": app.settings.comms_humans_enabled,
            "relay": app.settings.comms_relay,
            "totals": app.comms.stats(),
            "rooms": [
                {"room": row["name"], "id": row["id"], **app.comms.stats(row["id"])}
                for row in rooms
            ],
        }, indent=2))
        return 0
    if verb == "keys":
        agent = app.store.one("select id, name from agents where id = ? or name = ?",
                              (args.agent, args.agent))
        if not agent:
            raise CritterTownError(f"agent {args.agent} not found")
        identity = app.comms.identity(agent["id"])
        print(json.dumps({"agent": agent["name"], **identity}, indent=2))
        return 0
    if verb == "join":
        for agent_id in args.agents:
            agent = app.store.one("select id from agents where id = ? or name = ?",
                                  (agent_id, agent_id))
            if not agent:
                raise CritterTownError(f"agent {agent_id} not found")
            app.comms.add_member(args.room, agent["id"], "agent", args.role)
        print(json.dumps({"room": args.room, "members": app.comms.members(args.room)}, indent=2))
        return 0
    if verb == "say":
        author = None
        if args.agent:
            row = app.store.one("select id from agents where id = ? or name = ?",
                                (args.agent, args.agent))
            author = row["id"] if row else None
        print(json.dumps(app.comms.say(args.room, args.body, author_id=author), indent=2))
        return 0
    if verb == "tail":
        events = app.comms.read(args.room, since_seq=args.since, limit=args.limit)
        for event in events:
            print(f"{event['seq']:>5} {event['name']:<14} "
                  f"{(event['author_id'] or event['npub'][:16]):<22} {event['content'][:80]}")
        return 0
    if verb == "verify":
        print(json.dumps(app.comms.verify(args.event_id), indent=2))
        return 0
    if verb == "bridge":
        print(json.dumps(app.comms.register_bridge(args.room, args.transport,
                                                  {"relay": args.relay, "url": args.url}
                                                  if (args.relay or args.url) else {}), indent=2))
        return 0
    if verb == "sync":
        print(json.dumps(app.comms.sync(args.room, args.transport), indent=2))
        return 0
    raise CritterTownError(f"unknown comms verb {verb}")


def cmd_import_agent(args: argparse.Namespace) -> int:
    """bring an agent in from a file, a directory or a url, in whatever format it is."""
    from agents.agent_imports import import_agent

    app = _app(args)
    team_id = args.team or app.crews.list_teams()[0]["id"]
    manifest = import_agent(args.source, source=args.format or "",
                            agent_name=args.entry or "")
    if args.dry_run_manifest:
        print(json.dumps(manifest.to_dict(), indent=2))
        return 0
    from agents.agent_imports import adopt

    outcome = adopt(app, team_id, manifest, name=args.name or "")
    print(json.dumps({"agent": outcome["agent"]["id"], "name": outcome["agent"]["name"],
                      "imported_from": outcome["imported_from"],
                      "warnings": outcome["warnings"]}, indent=2))
    return 0


def cmd_export_agent(args: argparse.Namespace) -> int:
    from agents.agent_imports import export_agent

    app = _app(args)
    agent = app.crews.get_agent(args.agent_id)
    profile = app.profile.profile(agent["id"]) if app.profile is not None else {}
    print(json.dumps(export_agent(agent, profile), indent=2))
    return 0


def cmd_agent_formats(args: argparse.Namespace) -> int:
    from agents.agent_imports import AGENT_SOURCES

    print(json.dumps(AGENT_SOURCES, indent=2))
    return 0


def cmd_attest(args: argparse.Namespace) -> int:
    """print an agent's signed trail, and say whether it verifies."""
    app = _app(args)
    if args.verify_file:
        bundle = json.loads(open(args.verify_file, encoding="utf-8").read())
        from manage import attest

        print(json.dumps(attest.verify_entries(bundle.get("entries") or []), indent=2))
        return 0
    if not args.agent_id:
        raise CritterTownError("pass an agent id, or --verify <bundle.json>")
    print(json.dumps(app.audit.export(args.agent_id), indent=2))
    return 0


def cmd_principal(args: argparse.Namespace) -> int:
    app = _app(args)
    print(json.dumps(app.audit.paired(args.agent_id), indent=2))
    return 0


def cmd_teams(args: argparse.Namespace) -> int:
    app = _app(args)
    if args.join:
        print(json.dumps(app.crews.join_team(args.agent_id, args.join, role=args.role or ""),
                         indent=2))
        return 0
    if args.leave:
        print(json.dumps(app.crews.leave_team(args.agent_id, args.leave), indent=2))
        return 0
    print(json.dumps(app.crews.teams_of(args.agent_id), indent=2))
    return 0


def cmd_persistence(args: argparse.Namespace) -> int:
    app = _app(args)
    print(json.dumps(app.crews.set_persistence(args.agent_id, args.mode), indent=2))
    return 0


def cmd_browser(args: argparse.Namespace) -> int:
    app = _app(args)
    if args.action == "providers":
        print(json.dumps(app.browsers.status(), indent=2))
        return 0
    if args.action == "list":
        print(json.dumps(app.browsers.list(args.agent_id or None), indent=2))
        return 0
    if args.action == "close":
        print(json.dumps(app.browsers.close(args.session_id), indent=2))
        return 0
    if args.action == "run":
        print(json.dumps(app.browsers.run(args.agent_id, args.task or "", task_id=args.task_id),
                         indent=2))
        return 0
    print(json.dumps(app.browsers.open(args.agent_id, task_id=args.task_id), indent=2))
    return 0


# uvicorn takes the app as an import string, so a wrong module here fails only when a
# person runs `crittertown serve`, and never in the suite. test_boundaries imports
# this constant so the path cannot rot again.
SERVE_TARGET = "api.routes:create_api"


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    os.environ.setdefault("CRITTERTOWN_DB", args.db or "crittertown.db")
    uvicorn.run(SERVE_TARGET, factory=True, host=args.host, port=args.port, reload=args.reload)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crittertown", description="agent crews with computers and workflows")
    parser.add_argument("--db", help="sqlite path (default $CRITTERTOWN_DB or crittertown.db)")
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
    bring = sub.add_parser("import-agent", help="adopt an agent from a file, directory or url")
    bring.add_argument("source")
    bring.add_argument("--team", help="crew id (default: the first crew)")
    bring.add_argument("--format", help="force a format instead of detecting it")
    bring.add_argument("--entry", help="which agent to take from a multi agent config")
    bring.add_argument("--name", help="store it under a different name")
    bring.add_argument("--manifest", dest="dry_run_manifest", action="store_true",
                       help="print the manifest instead of creating the agent")
    bring.set_defaults(func=cmd_import_agent)
    take = sub.add_parser("export-agent", help="print an agent as a portable manifest")
    take.add_argument("agent_id")
    take.set_defaults(func=cmd_export_agent)
    sub.add_parser("agent-formats", help="list the agent formats this can read").set_defaults(
        func=cmd_agent_formats)
    take_attest = sub.add_parser("attest", help="print or verify a signed trail")
    take_attest.add_argument("agent_id", nargs="?")
    take_attest.add_argument("--verify", dest="verify_file", help="verify a bundle from disk")
    take_attest.set_defaults(func=cmd_attest)
    principal = sub.add_parser("principal", help="the agentid, its key, and both proofs")
    principal.add_argument("agent_id")
    principal.set_defaults(func=cmd_principal)
    teams = sub.add_parser("teams", help="crews an agent works in")
    teams.add_argument("agent_id")
    teams.add_argument("--join", help="crew id to join")
    teams.add_argument("--leave", help="crew id to leave")
    teams.add_argument("--role")
    teams.set_defaults(func=cmd_teams)
    persistence = sub.add_parser("persistence", help="workspace or task_state")
    persistence.add_argument("agent_id")
    persistence.add_argument("mode")
    persistence.set_defaults(func=cmd_persistence)
    browser = sub.add_parser("browser", help="open, list, run or close an agent browser")
    browser.add_argument("action", nargs="?", default="open",
                         choices=["open", "list", "close", "run", "providers"])
    browser.add_argument("agent_id", nargs="?")
    browser.add_argument("--task", help="the goal to run when action is run")
    browser.add_argument("--task-id")
    browser.add_argument("--session", dest="session_id")
    browser.set_defaults(func=cmd_browser)
    serve = sub.add_parser("serve", help="run the http api")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    comms = sub.add_parser("comms", help="the signed room log: keys, bridges, sync")
    comms_sub = comms.add_subparsers(dest="verb", required=True)
    comms_sub.add_parser("status", help="custody, relay, and per-room counts").set_defaults(func=cmd_comms)
    keys = comms_sub.add_parser("keys", help="print an agent's public key (never the secret)")
    keys.add_argument("agent")
    keys.set_defaults(func=cmd_comms)
    join = comms_sub.add_parser("join", help="make agents members of a room")
    join.add_argument("room")
    join.add_argument("agents", nargs="+")
    join.add_argument("--role", default="member")
    join.set_defaults(func=cmd_comms)
    say = comms_sub.add_parser("say", help="sign and post a room message")
    say.add_argument("room")
    say.add_argument("body")
    say.add_argument("--agent")
    say.set_defaults(func=cmd_comms)
    tail = comms_sub.add_parser("tail", help="print a room's log")
    tail.add_argument("room")
    tail.add_argument("--since", type=int, default=0)
    tail.add_argument("--limit", type=int, default=20)
    tail.set_defaults(func=cmd_comms)
    verify = comms_sub.add_parser("verify", help="check one event's signature")
    verify.add_argument("event_id")
    verify.set_defaults(func=cmd_comms)
    bridge = comms_sub.add_parser("bridge", help="attach a relay or http bridge to a room")
    bridge.add_argument("room")
    bridge.add_argument("transport", choices=["http", "nostr", "relay"])
    bridge.add_argument("--relay")
    bridge.add_argument("--url")
    bridge.set_defaults(func=cmd_comms)
    sync = comms_sub.add_parser("sync", help="push and pull a room through its bridges")
    sync.add_argument("room")
    sync.add_argument("transport", nargs="?", default=None)
    sync.set_defaults(func=cmd_comms)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CritterTownError as exc:
        print(json.dumps(exc.to_dict(), indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
