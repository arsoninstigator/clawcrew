"""agent runtime: how an agent thinks, independent of how it runs.

`AgentModel` is the seam. the default `DeterministicModel` needs no network and
is what tests use, and what a self-hoster gets before wiring a model. the
`OpenAICompatibleModel` talks to any /chat/completions endpoint (openai, together,
groq, ollama, vllm) so no vendor is baked in.

agents return a small json action envelope, parsed here, because an agent loop
that echoes free text cannot be scheduled or reviewed:

    {"thought": "...", "commands": ["npm test"], "message": {"to": "...",
     "kind": "result", "body": "..."}, "done": true, "result": {...}}
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.config import Settings
from core.errors import ProviderFailure
from sandbox.http import request_json

ACTION_SCHEMA = """respond with a single json object, no prose outside it:
{"thought": "one line of reasoning",
 "commands": ["shell command to run in your computer"],
 "message": {"to": "agent_name or null", "kind": "note|handoff|request|result|blocker", "body": "..."},
 "done": false,
 "result": {}}
at most 3 commands per turn. set done=true only when the task is finished."""


SANDBOX_ENV_PATH = "/agents/{agent_id}/sandbox-env"


def pull_env(base_url: str, agent_id: str, token: str = "", timeout: int = 10,
             apply: bool = True) -> dict:
    """fetch the env the platform holds for this agent, at boot.

    a computer's create request has no env field, so this is how a proxied credential
    reaches the harness: over the authenticated channel, into this process's
    environment. the return value carries names and a count, never values, because
    anything returned here tends to end up in a run row.
    """
    if not base_url:
        return {"loaded": 0, "names": [], "skipped": "no platform url"}
    url = base_url.rstrip("/") + SANDBOX_ENV_PATH.format(agent_id=agent_id)
    try:
        payload = request_json(
            "GET", url,
            headers={"authorization": f"Bearer {token}"} if token else {},
            timeout=timeout,
        )
    except Exception as exc:  # a boot must not depend on the broker being reachable
        return {"loaded": 0, "names": [], "error": str(exc)[:200]}
    env = {k: v for k, v in ((payload or {}).get("env") or {}).items() if isinstance(v, str)}
    if apply:
        os.environ.update(env)
    return {"loaded": len(env), "names": sorted(env)}


class AgentModel(Protocol):
    def act(self, system_prompt: str, prompt: str) -> dict: ...


@dataclass
class DeterministicModel:
    """no-network model used by tests and by `--dry-run`.

    it reads the task's own instruction and produces one shell command per call,
    which is enough to prove the loop, the sandbox, the kanban and the a2a gate
    all work end to end without spending a token.
    """

    script: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self._turn = 0

    def act(self, system_prompt: str, prompt: str) -> dict:
        if self.script:
            idx = min(self._turn, len(self.script) - 1)
            self._turn += 1
            return dict(self.script[idx])
        self._turn += 1
        if self._turn == 1:
            return {
                "thought": "inspect the workspace before acting",
                "commands": ["pwd"],
                "message": None,
                "done": False,
                "result": {},
            }
        return {
            "thought": "workspace inspected, task finished",
            "commands": [],
            "message": {
                "to": None,
                "kind": "result",
                "body": "task completed by deterministic runtime (no model configured)",
            },
            "done": True,
            "result": {"summary": "deterministic run finished", "turns": self._turn},
        }


@dataclass
class OpenAICompatibleModel:
    base_url: str
    api_key: str = ""
    model: str = "gpt-4.1-mini"
    max_tokens: int = 1200
    timeout: int = 120

    def act(self, system_prompt: str, prompt: str) -> dict:
        if not self.base_url:
            raise ProviderFailure("no model endpoint configured")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": f"{system_prompt}\n\n{ACTION_SCHEMA}"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": self.max_tokens,
        }
        raw = request_json(
            "POST",
            self.base_url.rstrip("/") + "/chat/completions",
            payload=payload,
            headers={"authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        content = (
            raw.get("choices", [{}])[0].get("message", {}).get("content", "")
            if isinstance(raw, dict)
            else ""
        )
        return parse_action(content)


def parse_action(text: str) -> dict:
    """tolerate fenced blocks and stray prose around the json object."""
    if not text:
        return {"thought": "empty model response", "commands": [], "done": False, "result": {}}
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.S)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        obj = json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            return {"thought": cleaned[:200], "commands": [], "done": False, "result": {}}
        try:
            obj = json.loads(cleaned[start : end + 1])
        except ValueError:
            return {"thought": cleaned[:200], "commands": [], "done": False, "result": {}}
    if not isinstance(obj, dict):
        return {"thought": "model returned a non object", "commands": [], "done": False, "result": {}}
    return normalize_action(obj)


def normalize_action(obj: dict) -> dict:
    commands = obj.get("commands") or []
    if isinstance(commands, str):
        commands = [commands]
    commands = [c for c in commands if isinstance(c, str) and c.strip()][:3]
    message = obj.get("message")
    if not isinstance(message, dict):
        message = None
    elif not message.get("body"):
        message = None
    result = obj.get("result")
    if not isinstance(result, dict):
        result = {"value": result} if result not in (None, "") else {}
    return {
        "thought": str(obj.get("thought", ""))[:2000],
        "commands": commands,
        "message": message,
        "done": bool(obj.get("done")),
        "result": result,
    }


def build_model(settings: Settings, override: dict | None = None) -> AgentModel:
    override = override or {}
    if override.get("kind") == "deterministic":
        return DeterministicModel()
    if settings.llm_base_url or override.get("base_url"):
        return OpenAICompatibleModel(
            base_url=override.get("base_url") or settings.llm_base_url,
            api_key=override.get("api_key") or settings.llm_api_key,
            model=override.get("model") or settings.llm_model,
        )
    return DeterministicModel()


def render_prompt(task: dict, agent: dict, crew: dict, context: dict, inbox: list[dict],
                  intent: str = "") -> str:
    """the prompt a run starts from. `intent` goes above the task on purpose: an agent
    that reads the task first optimises the task, and the point of the intent column is
    that the crew's direction is louder than the card."""
    lines = [
        f"crew: {crew['name']} ({crew['mission'] or 'no mission set'})",
    ]
    if intent:
        lines.append(intent)
    lines += [
        f"you are: {agent['name']} ({agent['role'] or 'generalist'})",
        f"task: {task['title']}",
        f"task id: {task['id']}",
        f"kanban column: {task['column']}",
    ]
    if task.get("description"):
        lines.append(f"details: {task['description']}")
    if task.get("payload"):
        lines.append(f"payload: {json.dumps(task['payload'], sort_keys=True)}")
    if context:
        lines.append("shared crew context:")
        for key, value in list(context.items())[:20]:
            lines.append(f"  {key} = {json.dumps(value, sort_keys=True)[:400]}")
    if inbox:
        lines.append("approved agent messages:")
        for message in inbox[:10]:
            lines.append(f"  [{message['kind']}] from {message['from_agent_id']}: {message['body'][:300]}")
    lines.append("work inside your computer. finish the task, then answer with the json envelope.")
    return "\n".join(lines)
