"""a2a protocol compatibility.

the bus in `a2a.py` is the product decision (nothing reaches an agent unreviewed).
this module is the wire format, written against the A2A specification
(a2aproject/A2A, Apache-2.0, `specification/a2a.proto`), so a clawcrew agent can be
reached by any a2a client and clawcrew can reach any a2a server.

json shapes follow the published a2a json schema: lowerCamelCase field names and
lowercase enum values (`"working"`, `"agent"`), which is what the schema in
a2aproject/A2A defines and what `clawcrew/vendor/a2a_types` validates against. the
proto spellings (`TASK_STATE_WORKING`, `ROLE_AGENT`) are still accepted on input
because older clients emit them, but they never leave this module. the two extra
things clawcrew keeps in `metadata` are additive and ignorable by a strict peer:

    metadata.clawcrew = {"agentId", "crewId", "status", "kind", "reviewedBy"}

that is deliberate: review state travels with the message, but a peer that only
understands the spec still reads a valid `Message` or `Task`.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import ValidationError

PROTOCOL_VERSION = "1.0"
PROTOCOL_BINDING = "JSONRPC"
DEFAULT_INPUT_MODES = ["text/plain", "application/json"]
DEFAULT_OUTPUT_MODES = ["text/plain", "application/json"]

ROLE_USER = "user"
ROLE_AGENT = "agent"
_ROLES = {
    "user": ROLE_USER,
    "role_user": ROLE_USER,
    "agent": ROLE_AGENT,
    "role_agent": ROLE_AGENT,
}

# kanban column -> TaskState. review is input-required because it waits on a human.
TASK_STATE_BY_COLUMN = {
    "backlog": "submitted",
    "ready": "submitted",
    "in_progress": "working",
    "review": "input-required",
    "blocked": "failed",
    "done": "completed",
}
TERMINAL_STATES = {"completed", "failed", "canceled", "rejected", "unknown"}

# proto spelling -> json schema value, for clients that speak protojson
_PROTO_STATES = {
    "TASK_STATE_SUBMITTED": "submitted",
    "TASK_STATE_WORKING": "working",
    "TASK_STATE_COMPLETED": "completed",
    "TASK_STATE_FAILED": "failed",
    "TASK_STATE_CANCELED": "canceled",
    "TASK_STATE_INPUT_REQUIRED": "input-required",
    "TASK_STATE_REJECTED": "rejected",
    "TASK_STATE_AUTH_REQUIRED": "auth-required",
    "TASK_STATE_UNSPECIFIED": "unknown",
}
_JSON_STATES = {
    "submitted": "submitted",
    "working": "working",
    "completed": "completed",
    "failed": "failed",
    "canceled": "canceled",
    "cancelled": "canceled",
    "input-required": "input-required",
    "input_required": "input-required",
    "rejected": "rejected",
    "auth-required": "auth-required",
    "auth_required": "auth-required",
    "unknown": "unknown",
}


def task_state(column: str) -> str:
    return TASK_STATE_BY_COLUMN.get(column, "working")


def normalize_role(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return _ROLES.get(raw, ROLE_AGENT)


def normalize_state(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.upper() in _PROTO_STATES:
        return _PROTO_STATES[raw.upper()]
    return _JSON_STATES.get(raw.lower(), "working")


# ---- parts -------------------------------------------------------------
def to_parts(body: str, media_type: str = "text/plain") -> list[dict]:
    return [{"text": body, "mediaType": media_type}]


def from_parts(parts: Any) -> str:
    """flatten any part list to the text body our bus stores."""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list) or not parts:
        raise ValidationError("a2a message needs at least one part")
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            raise ValidationError("each part must be an object")
        if part.get("text"):
            chunks.append(str(part["text"]))
        elif part.get("data") is not None:
            chunks.append(json.dumps(part["data"], sort_keys=True))
        elif part.get("url"):
            chunks.append(str(part["url"]))
        elif part.get("raw"):
            raise ValidationError("raw byte parts are not accepted by this endpoint")
    body = "\n".join(chunk for chunk in chunks if chunk)
    if not body.strip():
        raise ValidationError("a2a message parts were empty")
    return body


# ---- message -----------------------------------------------------------
def message_to_json(row: dict, role: str = ROLE_AGENT) -> dict:
    metadata = {
        "clawcrew": {
            "agentId": row.get("from_agent_id"),
            "crewId": row.get("team_id"),
            "status": row.get("status"),
            "kind": row.get("kind"),
            "reviewedBy": row.get("reviewed_by") or "",
            "toAgentId": row.get("to_agent_id"),
        }
    }
    if row.get("review_note"):
        metadata["clawcrew"]["reviewNote"] = row["review_note"]
    payload = {
        "kind": "message",
        "messageId": row["id"],
        "contextId": row.get("thread_id") or "",
        "role": role,
        "parts": to_parts(row.get("body", "")),
        "metadata": metadata,
    }
    if row.get("task_id"):
        payload["taskId"] = row["task_id"]
    return payload


def message_from_json(payload: dict) -> dict:
    """read an inbound a2a Message into the fields our bus needs.

    sender identity travels in `metadata.clawcrew.agentId` because a2a's `role`
    only says client or server, not which agent. without it we cannot attribute
    the message, and an unattributable message cannot be reviewed.
    """
    if not isinstance(payload, dict):
        raise ValidationError("a2a message must be an object")
    if payload.get("kind") not in (None, "message"):
        raise ValidationError("expected a Message, got kind=" + str(payload.get("kind")))
    body = from_parts(payload.get("parts"))
    metadata = payload.get("metadata") or {}
    claw = metadata.get("clawcrew") or {}
    sender = claw.get("agentId") or metadata.get("agentId")
    if not sender:
        raise ValidationError(
            "inbound message needs metadata.clawcrew.agentId so it can be attributed and reviewed"
        )
    return {
        "from_agent_id": sender,
        "to_agent_id": claw.get("toAgentId"),
        "task_id": payload.get("taskId") or claw.get("taskId"),
        "thread_id": payload.get("contextId") or payload.get("threadId") or None,
        "kind": claw.get("kind") or "note",
        "body": body,
        "message_id": payload.get("messageId"),
        "role": normalize_role(payload.get("role")),
    }


# ---- task --------------------------------------------------------------
def task_to_json(task: dict, messages: list[dict] | None = None) -> dict:
    messages = messages or []
    status = {
        "state": task_state(task["column"]),
        "timestamp": _iso(task.get("updated_date")),
    }
    if task.get("review_note"):
        status["message"] = {"kind": "message", "role": ROLE_AGENT,
                             "messageId": f"{task['id']}:status",
                             "parts": to_parts(task["review_note"])}
    payload = {
        "kind": "task",
        "id": task["id"],
        "contextId": task.get("parent_id") or task["id"],
        "status": status,
        "artifacts": _artifacts(task),
        "history": [message_to_json(m) for m in messages],
        "metadata": {
            "clawcrew": {
                "crewId": task["team_id"],
                "column": task["column"],
                "agentId": task.get("assignee_agent_id"),
                "isolation": task.get("isolation"),
                "priority": task.get("priority"),
            }
        },
    }
    return payload


def _artifacts(task: dict) -> list[dict]:
    result = task.get("result") or {}
    if not result:
        return []
    return [
        {
            "artifactId": f"{task['id']}:result",
            "name": "result",
            "parts": [{"data": result, "mediaType": "application/json"}],
        }
    ]


# ---- agent card --------------------------------------------------------
def agent_card(
    crew: dict,
    agent: dict,
    base_url: str,
    review_mode: str = "reviewed",
    version: str = "0.1.0",
) -> dict:
    url = base_url.rstrip("/") + "/a2a/rpc"
    skills = []
    for index, skill in enumerate(agent.get("skills") or []):
        skills.append(
            {
                "id": f"{agent['id']}:{index}",
                "name": str(skill),
                "description": f"{agent['name']} can {skill}",
                "tags": [str(skill)],
            }
        )
    if not skills:
        skills.append(
            {
                "id": agent["id"],
                "name": agent.get("role") or "generalist",
                "description": agent.get("role") or "generalist agent",
                "tags": ["generalist"],
            }
        )
    return {
        "name": agent["name"],
        "description": f"{agent.get('role') or 'agent'} on the {crew['name']} crew",
        "version": version,
        "protocolVersion": PROTOCOL_VERSION,
        # newer revisions moved the endpoint into supportedInterfaces; both are
        # emitted so a peer on either revision finds what it looks for
        "supportedInterfaces": [
            {"url": url, "protocolBinding": PROTOCOL_BINDING, "protocolVersion": PROTOCOL_VERSION}
        ],
        "url": url,  # v0.3 carries the endpoint here
        "preferredTransport": PROTOCOL_BINDING,  # v0.3 transport selector
        "provider": {"organization": "clawcrew", "url": "https://github.com/clawcrew"},
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extensions": [
                {
                    "uri": "https://clawcrew.dev/extensions/review-gate",
                    "description": "messages are held pending review when the crew review mode is 'reviewed'",
                    "required": False,
                    "params": {"reviewMode": review_mode},
                }
            ],
        },
        "defaultInputModes": DEFAULT_INPUT_MODES,
        "defaultOutputModes": DEFAULT_OUTPUT_MODES,
        "skills": skills,
        "metadata": {"clawcrew": {"agentId": agent["id"], "crewId": crew["id"]}},
    }


# ---- json-rpc ----------------------------------------------------------
def rpc_result(rpc_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def rpc_error(rpc_id: Any, code: int, message: str, data: Any = None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


RPC_PARSE_ERROR = -32700
RPC_INVALID_REQUEST = -32600
RPC_METHOD_NOT_FOUND = -32601
RPC_INVALID_PARAMS = -32602
RPC_INTERNAL = -32603


def _iso(ts: Any) -> str:
    from datetime import datetime, timezone

    if not ts:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat().replace("+00:00", "Z")


# ---- conformance -------------------------------------------------------
WIRE_KINDS = ("agent_card", "message", "task")


def validate_wire(kind: str, payload: dict) -> dict:
    """round trip a payload through the a2a models vendored from the official sdk
    (`clawcrew/vendor/a2a_types`, generated from `specification/json/a2a.json`).

    a protocol layer that only agrees with itself is worthless, so this is the check
    that keeps the wire format honest: if the vendored models reject a payload we
    emit, the bug is ours. returns the model dump (spec spellings, camelCase keys)
    and raises ValidationError with the model's reasons when it does not fit.
    """
    from pydantic import ValidationError as PydanticValidationError

    from .vendor.a2a_types import types as a2a_types

    models = {"agent_card": "AgentCard", "message": "Message", "task": "Task"}
    name = models.get(kind)
    if name is None:
        raise ValidationError(f"unknown a2a payload kind '{kind}'", allowed=list(WIRE_KINDS))
    model = getattr(a2a_types, name)
    try:
        validated = model.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError(
            f"payload is not a valid a2a {name}",
            errors=[str(item.get("msg")) for item in exc.errors()],
        ) from exc
    return validated.model_dump(mode="json", by_alias=True, exclude_none=True)
