"""a2a, read two ways.

the wire follows the **A2A specification** (a2aproject/A2A, now under the linux
foundation): json-rpc 2.0, `Message` with `parts`, `Task` with `status.state`, agent
cards published at the well-known path, sse for streaming. `clawcrew/a2a_protocol.py`
already emits those shapes and the vendored `a2a_types` validates them.

a human needs a different projection of the same rows: who spoke, to whom, about
which card, in what state, and whether the owner is still holding it. this module
renders that sentence *and* keeps the wire envelope beside it, so a disagreement
between the two is visible instead of resolved by trusting whichever is prettier.

the protocol landscape, since "which protocol" was a real question:

  A2A (linux foundation)  json-rpc, agent cards, tasks/messages/artifacts, sse.
                          the interop default. this is what the wire speaks.
  ACP (agntcy)            rest/openapi, runs + threads, audit leaning. clawcrew
                          serves a rest read path over the same rows so an
                          acp-minded client has something to call.
  ANP                     did-based discovery for an open agent mesh. out of scope:
                          this is one deployment's crew, not a public network.
  MCP                     agent to tool, not agent to agent. different layer.

so: one event log, one canonical wire (A2A), one rest read adapter, and a human
rendering that never becomes the source of truth.
"""

from __future__ import annotations

from typing import Any

from . import a2a_protocol as spec
from .db import loads
from .errors import NotFound

KIND_WORDS = {
    "note": "says",
    "handoff": "hands over",
    "request": "asks for",
    "result": "reports back",
    "blocker": "is blocked",
    "review_request": "asks for review",
}


def _clock(ts: float | None) -> str:
    if not ts:
        return ""
    import time

    return time.strftime("%H:%M", time.gmtime(ts))


def agents_index(crews, team_id: str) -> dict[str, dict]:
    return {
        agent["id"]: {
            "agent_id": agent["id"],
            "agent_key": agent.get("agent_key", ""),
            "name": agent["name"],
            "role": agent["role"],
        }
        for agent in crews.list_agents(team_id)
    }


def render_message(
    message: dict,
    agents: dict[str, dict],
    card_title: str | None = None,
    team_name: str = "",
) -> dict:
    """one message, three readings: a sentence, the envelope, and the review state."""
    sender = agents.get(message.get("from_agent_id") or "") or {
        "name": message.get("from_agent_id") or "unknown",
        "agent_key": "",
    }
    recipient = agents.get(message.get("to_agent_id") or "") or (
        {"name": "the crew", "agent_key": ""} if not message.get("to_agent_id") else
        {"name": message.get("to_agent_id") or "unknown", "agent_key": ""}
    )
    verb = KIND_WORDS.get(message.get("kind", "note"), "says")
    about = f" about “{card_title}”" if card_title else ""
    body = (message.get("body") or "").strip()
    sentence = f"{sender['name']} {verb} {recipient['name']}{about}: {body}"
    if message.get("status") == "pending":
        sentence += "  (waiting on owner review)"
    elif message.get("status") == "rejected":
        sentence += "  (rejected in review)"
    wire = spec.message_to_json(message, role=spec.ROLE_AGENT)
    wire.setdefault("metadata", {})
    wire["metadata"]["clawcrew"] = {
        "agentId": sender.get("agent_id") or message.get("from_agent_id"),
        "agentKey": sender.get("agent_key", ""),
        "recipientId": message.get("to_agent_id"),
        "status": message.get("status"),
        "kind": message.get("kind"),
        "reviewedBy": message.get("reviewed_by") or "",
        "crew": team_name,
    }
    return {
        "id": message.get("id"),
        "thread_id": message.get("thread_id"),
        "human": sentence,
        "line": f"{_clock(message.get('created_date'))}  {sentence}".strip(),
        "status": message.get("status"),
        "from": sender,
        "to": recipient,
        "task_id": message.get("task_id"),
        "wire": wire,
    }


def transcript(
    a2a_service,
    crews,
    team_id: str,
    kanban=None,
    limit: int = 100,
    include_pending: bool = True,
) -> dict:
    """the god view: every thread, the human sentences, and the envelopes.

    `include_pending` defaults to true on purpose. an owner wants to see what is
    waiting on them *before* it reaches an agent, so the god view is the one place
    where unreviewed traffic is visible.
    """
    team = crews.get_team(team_id)
    agents = agents_index(crews, team_id)
    statuses = ("approved", "pending", "rejected") if include_pending else ("approved",)
    rows = a2a_service.store.query(
        f"""select * from a2a_messages where team_id = ?
            and status in ({','.join('?' * len(statuses))})
            order by created_date desc limit ?""",
        (team_id, *statuses, limit),
    )
    titles: dict[str, str] = {}
    if kanban is not None:
        for row in rows:
            task_id = row["task_id"]
            if task_id and task_id not in titles:
                try:
                    titles[task_id] = kanban.get_task(task_id)["title"]
                except NotFound:
                    titles[task_id] = ""

    threads: dict[str, list[dict]] = {}
    for row in reversed(list(rows)):
        message = {
            key: row[key]
            for key in (
                "id", "team_id", "task_id", "from_agent_id", "to_agent_id", "kind",
                "body", "status", "reviewed_by", "review_note", "thread_id", "created_date",
                "reviewed_date",
            )
        }
        rendered = render_message(
            message, agents, card_title=titles.get(message.get("task_id") or ""),
            team_name=team["name"],
        )
        threads.setdefault(message["thread_id"] or "unthreaded", []).append(rendered)

    return {
        "team_id": team_id,
        "team": {"id": team["id"], "name": team["name"], "review_mode": team["review_mode"]},
        "protocol": {
            "wire": "A2A",
            "version": spec.PROTOCOL_VERSION,
            "binding": spec.PROTOCOL_BINDING,
            "agent_card_path": "/a2a/agents/{agent_key}/card",
            "rest_read_path": "/a2a/threads",
        },
        "counts": {
            "threads": len(threads),
            "messages": sum(len(items) for items in threads.values()),
            "pending": sum(1 for items in threads.values() for i in items if i["status"] == "pending"),
        },
        "threads": [
            {
                "thread_id": thread_id,
                "messages": items,
                "digest": [item["line"] for item in items],
            }
            for thread_id, items in threads.items()
        ],
    }


def thread_view(a2a_service, crews, thread_id: str, kanban=None) -> dict:
    rows = a2a_service.thread(thread_id)
    if not rows:
        raise NotFound(f"thread {thread_id} not found")
    team_id = rows[0]["team_id"]
    agents = agents_index(crews, team_id)
    titles = {}
    for message in rows:
        task_id = message.get("task_id")
        if task_id and kanban is not None and task_id not in titles:
            try:
                titles[task_id] = kanban.get_task(task_id)["title"]
            except NotFound:
                titles[task_id] = ""
    rendered = [
        render_message(message, agents, card_title=titles.get(message.get("task_id") or ""))
        for message in rows
    ]
    return {
        "thread_id": thread_id,
        "messages": rendered,
        "digest": [item["line"] for item in rendered],
        "as_text": "\n".join(item["line"] for item in rendered),
    }
