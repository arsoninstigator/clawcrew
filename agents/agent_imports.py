"""agent importers.

the rule this file exists for: an agent should not have to be born here. a crew you
already run somewhere else, a subagent file you wrote for another tool, an agent card
someone published, a prompt in a git repo - all of those are agents, and refusing them
because they were not typed into this api is the kind of lock-in that makes a platform
not worth adopting.

so: read the format, produce one **manifest** (crittertown's own portable description of an
agent), then adopt it. the manifest is the only thing adoption understands, which means
adding a format never touches the adoption path and exporting an agent is free.

formats read here:

    claude-code       one markdown file with yaml frontmatter, body is the prompt
    openclaw          a directory of markdown (AGENTS.md, SOUL.md, ...) plus skills/
    crewai            agents.yaml or json entries (role, goal, backstory, tools, llm)
    a2a               an a2a agent card json (name, description, skills, capabilities)
    openai            an assistants style object (name, instructions, model, tools)
    crittertown          our own manifest, so import and export are the same shape

what is deliberately not invented: a tool name a format has and crittertown has no
equivalent for stays in `unmapped` and shows up as a warning. a silent drop is how an
imported agent ends up with permissions nobody chose.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from core.errors import NotFound, ValidationError

SOURCE_CLAUDE_CODE = "claude-code"
SOURCE_OPENCLAW = "openclaw"
SOURCE_CREWAI = "crewai"
SOURCE_A2A = "a2a"
SOURCE_OPENAI = "openai-assistant"
SOURCE_CRITTERTOWN = "crittertown"

AGENT_SOURCES = {
    SOURCE_CLAUDE_CODE: "markdown + yaml frontmatter (.claude/agents/*.md style)",
    SOURCE_OPENCLAW: "a directory of markdown (AGENTS.md, SOUL.md, IDENTITY.md) plus skills/",
    SOURCE_CREWAI: "agents.yaml or agents.json entries (role, goal, backstory, tools, llm)",
    SOURCE_A2A: "an a2a agent card json",
    SOURCE_OPENAI: "an assistants style json object",
    SOURCE_CRITTERTOWN: "a crittertown agent manifest, which is also what export writes",
}

DOC_NAMES = {name.upper(): label for name, label in {
    "AGENTS.md": "instructions", "SOUL.md": "persona", "IDENTITY.md": "identity",
    "USER.md": "about-the-user", "MEMORY.md": "memory", "NOTES.md": "notes"}.items()}


@dataclass
class AgentManifest:
    """one agent, said once. everything else is a translation into this."""

    name: str
    role: str = ""
    instructions: str = ""
    model: str = ""
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    knowledge: list[dict] = field(default_factory=list)
    memory: dict = field(default_factory=dict)
    approval: dict = field(default_factory=dict)
    runtime: dict = field(default_factory=dict)
    channels: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    source: str = ""
    warnings: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kind": "agent",
            "name": self.name,
            "role": self.role,
            "instructions": self.instructions,
            "model": self.model,
            "skills": self.skills,
            "tools": self.tools,
            "knowledge": self.knowledge,
            "memory": self.memory,
            "approval": self.approval,
            "runtime": self.runtime,
            "channels": self.channels,
            "metadata": self.metadata,
            "source": self.source,
            "warnings": self.warnings,
            "unmapped": self.unmapped,
        }


# ---- frontmatter -------------------------------------------------------
def split_frontmatter(text: str) -> tuple[dict, str, list[str]]:
    """yaml frontmatter without requiring pyyaml.

    the subset that agent files actually use is `key: value`, `key:` followed by a
    `- item` list, and inline `[a, b]`. anything else is reported instead of guessed,
    because a quietly mis-parsed `tools:` list is a permissions bug.
    """
    warnings: list[str] = []
    if not text.startswith("---"):
        return {}, text, warnings
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text, ["frontmatter was opened but never closed, so it was treated as prose"]
    raw, body = parts[1], parts[2]
    data: dict[str, Any] = {}
    current_key = ""
    for line in raw.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and current_key:
            data.setdefault(current_key, [])
            if isinstance(data[current_key], list):
                data[current_key].append(_scalar(line.lstrip()[2:].strip()))
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            current_key = key.strip()
            value = value.strip()
            if not value:
                data[current_key] = []
            else:
                data[current_key] = _scalar(value)
            continue
        warnings.append(f"frontmatter line not understood: {line.strip()[:60]}")
    return data, body.lstrip("\n"), warnings


def _scalar(value: str) -> Any:
    value = value.strip().strip('"').strip("'")
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part) for part in inner.split(",")]
    low = value.lower()
    if low in {"true", "false"}:
        return low == "true"
    return value


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _slug(text: str) -> str:
    keep = "".join(ch if ch.isalnum() else "-" for ch in str(text).lower()).strip("-")
    while "--" in keep:
        keep = keep.replace("--", "-")
    return keep or "agent"


# ---- one format each ---------------------------------------------------
def from_claude_code(text: str, name_hint: str = "") -> AgentManifest:
    meta, body, warnings = split_frontmatter(text)
    name = str(meta.get("name") or name_hint or "").strip()
    if not name:
        raise ValidationError("a claude-code subagent file needs a name in its frontmatter")
    tools = _as_list(meta.get("tools")) or _as_list(meta.get("allowed-tools"))
    manifest = AgentManifest(
        name=name,
        role=str(meta.get("description") or "")[:200],
        instructions=body.strip(),
        model=str(meta.get("model") or ""),
        tools=tools,
        source=SOURCE_CLAUDE_CODE,
        warnings=warnings,
        metadata={"origin": "claude-code subagent", "raw_frontmatter": meta},
    )
    for key in meta:
        if key not in {"name", "description", "tools", "allowed-tools", "model"}:
            manifest.unmapped.append(f"frontmatter.{key}")
    return manifest


def from_openclaw(files: dict[str, str], name_hint: str = "") -> AgentManifest:
    """a directory of markdown: prose is the agent, so prose is what gets imported."""
    lowered = {os.path.basename(path).upper(): text for path, text in files.items()}
    if not any(key in lowered for key in ("AGENTS.MD", "SOUL.MD", "IDENTITY.MD")):
        raise ValidationError(
            "an openclaw style bundle needs at least one of AGENTS.md, SOUL.md or IDENTITY.md",
            found=sorted(lowered),
        )
    meta: dict[str, Any] = {}
    warnings: list[str] = []
    if "IDENTITY.MD" in lowered:
        identity_meta, _, identity_warnings = split_frontmatter(lowered["IDENTITY.MD"])
        meta.update(identity_meta)
        warnings.extend(identity_warnings)
    name = str(meta.get("name") or name_hint or "").strip()
    if not name:
        first_heading = ""
        for text in lowered.values():
            for line in text.splitlines():
                if line.strip().startswith("#"):
                    first_heading = line.strip("# ").strip()
                    break
            if first_heading:
                break
        name = first_heading or "imported agent"

    manifest = AgentManifest(name=name, source=SOURCE_OPENCLAW, warnings=warnings,
                             metadata={"origin": "openclaw style bundle",
                                       "files": sorted(lowered)})
    parts: list[str] = []
    for filename, text in sorted(lowered.items()):
        if not filename.endswith(".MD"):
            continue
        _, body, _ = split_frontmatter(text)
        role = DOC_NAMES.get(filename.upper(), "")
        if filename in {"AGENTS.MD", "SOUL.MD", "IDENTITY.MD"}:
            parts.append(f"## {role or filename}\n{body.strip()}")
        elif body.strip():
            manifest.knowledge.append({"title": f"{role or filename} (imported)",
                                       "text": body.strip()[:20000]})
    manifest.instructions = "\n\n".join(parts).strip()
    manifest.role = str(meta.get("role") or meta.get("title") or "")[:200]
    manifest.model = str(meta.get("model") or "")
    skills = sorted({os.path.basename(os.path.dirname(path))
                     for path in files
                     if os.path.basename(path).upper() in {"SKILL.MD", "SKILLS.MD"}})
    manifest.skills = [skill for skill in skills if skill and skill != "."]
    manifest.memory = dict(meta.get("memory") or {}) if isinstance(meta.get("memory"), dict) else {}
    for key in meta:
        if key not in {"name", "role", "title", "model", "memory"}:
            manifest.unmapped.append(f"identity.{key}")
    manifest.metadata["skill_sources"] = [path for path in files
                                          if os.path.basename(path).upper() == "SKILL.MD"]
    return manifest


def from_crewai(payload: Any, agent_name: str = "") -> AgentManifest:
    if isinstance(payload, str):
        payload = _parse_yaml(payload)
    entries = payload
    if isinstance(payload, dict):
        for key in ("agents", "crews", "agents_config"):
            if isinstance(payload.get(key), (dict, list)):
                entries = payload[key]
                break
    if isinstance(entries, dict):
        if agent_name and agent_name in entries:
            chosen = (agent_name, entries[agent_name])
        else:
            first = next(iter(entries.items()), None)
            if first is None:
                raise ValidationError("the crewai config has no agents")
            chosen = first
        entry_name, entry = chosen
    elif isinstance(entries, list) and entries:
        entry = entries[0]
        entry_name = str(entry.get("name") or entry.get("role") or "agent")
    else:
        raise ValidationError("no crewai agent could be found in this file")
    if not isinstance(entry, dict):
        raise ValidationError("a crewai agent entry must be an object")
    instructions: list[str] = []
    if entry.get("backstory"):
        instructions.append(f"## backstory\n{entry['backstory']}")
    if entry.get("goal"):
        instructions.append(f"## goal\n{entry['goal']}")
    if entry.get("instructions"):
        instructions.append(f"## instructions\n{entry['instructions']}")
    manifest = AgentManifest(
        name=str(entry.get("name") or entry_name),
        role=str(entry.get("role") or "")[:200],
        instructions="\n\n".join(instructions),
        model=str(entry.get("llm") or entry.get("model") or ""),
        tools=_as_list(entry.get("tools")),
        source=SOURCE_CREWAI,
        metadata={"origin": "crewai agent config", "entry": entry_name},
    )
    if entry.get("allow_delegation") is not None:
        manifest.approval["can_delegate"] = bool(entry.get("allow_delegation"))
    for key in ("max_iter", "max_rpm", "verbose", "type", "function_calling_llm",
                "memory", "cache", "system_template", "prompt_template"):
        if key in entry:
            manifest.unmapped.append(key)
    return manifest


def from_a2a(payload: Any) -> AgentManifest:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValidationError("an a2a agent card must be a json object")
    if not payload.get("name"):
        raise ValidationError("an a2a agent card needs a `name`")
    manifest = AgentManifest(
        name=str(payload["name"]),
        role=str(payload.get("description") or "")[:200],
        source=SOURCE_A2A,
        metadata={"origin": "a2a agent card", "url": payload.get("url", ""),
                  "version": payload.get("version", ""),
                  "default_input_modes": payload.get("defaultInputModes") or [],
                  "default_output_modes": payload.get("defaultOutputModes") or []},
    )
    for skill in payload.get("skills") or []:
        if not isinstance(skill, dict):
            continue
        skill_name = str(skill.get("id") or skill.get("name") or "").strip()
        if skill_name:
            manifest.skills.append(skill_name)
        description = str(skill.get("description") or "").strip()
        if description or skill.get("examples"):
            manifest.knowledge.append({
                "title": f"skill: {skill.get('name') or skill_name}",
                "text": "\n".join(filter(None, [
                    description,
                    "examples: " + ", ".join(str(e) for e in skill.get("examples") or [])
                    if skill.get("examples") else "",
                ])),
            })
    if payload.get("capabilities"):
        manifest.metadata["capabilities"] = payload["capabilities"]
    for key in ("url", "preferredTransport", "provider", "securitySchemes", "security",
                "protocolVersion", "signatures", "documentationUrl", "iconUrl"):
        if key in payload:
            manifest.unmapped.append(f"card.{key}")
    if not manifest.instructions:
        manifest.warnings.append(
            "an agent card describes what an agent can do, not how it thinks: "
            "no system prompt came with it, so the instructions are empty")
    return manifest


def from_openai(payload: Any) -> AgentManifest:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValidationError("an assistant object must be a json object")
    if not (payload.get("name") or payload.get("instructions")):
        raise ValidationError("an assistant object needs a name or instructions")
    tools: list[str] = []
    unmapped: list[str] = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        kind = str(tool.get("type") or "")
        if kind == "function" and isinstance(tool.get("function"), dict):
            tools.append(str(tool["function"].get("name") or "function"))
        elif kind.startswith("file_search") or kind == "code_interpreter" or kind == "computer_use":
            unmapped.append(f"tool.{kind}")
        else:
            tools.append(kind or "tool")
    manifest = AgentManifest(
        name=str(payload.get("name") or "imported assistant"),
        instructions=str(payload.get("instructions") or "").strip(),
        model=str(payload.get("model") or ""),
        tools=tools,
        source=SOURCE_OPENAI,
        metadata={"origin": "openai assistant object",
                  "id": payload.get("id", ""),
                  "tool_resources": payload.get("tool_resources") or {},
                  "metadata": payload.get("metadata") or {}},
        unmapped=unmapped,
    )
    for key in ("response_format", "temperature", "top_p", "reasoning_effort", "truncation"):
        if key in payload:
            manifest.unmapped.append(key)
    return manifest


def from_crittertown(payload: Any) -> AgentManifest:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict) or not payload.get("name"):
        raise ValidationError("a crittertown manifest needs a name")
    return AgentManifest(
        name=str(payload["name"]),
        role=str(payload.get("role") or ""),
        instructions=str(payload.get("instructions") or ""),
        model=str(payload.get("model") or ""),
        skills=_as_list(payload.get("skills")),
        tools=_as_list(payload.get("tools")),
        knowledge=list(payload.get("knowledge") or []),
        memory=dict(payload.get("memory") or {}),
        approval=dict(payload.get("approval") or {}),
        runtime=dict(payload.get("runtime") or {}),
        channels=_as_list(payload.get("channels")),
        metadata=dict(payload.get("metadata") or {}),
        source=SOURCE_CRITTERTOWN,
    )


def _parse_yaml(text: str) -> Any:
    try:
        import yaml  # type: ignore
    except ImportError:
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ValidationError(
                "crewai configs are usually yaml; install pyyaml or pass the json export",
                hint="pip install pyyaml",
            ) from exc
    try:
        return yaml.safe_load(text) or {}
    except Exception as exc:  # noqa: BLE001 - yaml has its own error types
        raise ValidationError(f"could not parse the yaml: {exc}") from exc


# ---- reading a source --------------------------------------------------
def _fetch(url: str, fetcher=None) -> str:
    if fetcher is not None:
        body = fetcher(url)
        return body if isinstance(body, str) else json.dumps(body)
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"accept": "text/plain, application/json, text/markdown"}),
                timeout=30) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise ValidationError(f"could not fetch {url}: http {exc.code}") from exc
    except (urllib.error.URLError, ValueError) as exc:
        raise ValidationError(f"could not fetch {url}: {exc}") from exc


def read_source(ref: str, fetcher=None) -> Any:
    """a path, a url, or the file's own text. returns text, parsed json, or a bundle
    of `{relative path: text}` for a directory."""
    if not isinstance(ref, str) or not ref.strip():
        raise ValidationError("nothing to import: give a path, a url or the content itself")
    text = ref.strip()
    if text.startswith(("http://", "https://")):
        body = _fetch(text, fetcher)
        stripped = body.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(body)
            except ValueError:
                return body
        return body
    if os.path.isdir(text):
        files: dict[str, str] = {}
        for root, dirs, names in os.walk(text):
            dirs[:] = [d for d in sorted(dirs) if not d.startswith(".") and d != "__pycache__"]
            for filename in sorted(names):
                if not filename.lower().endswith((".md", ".txt", ".json", ".yaml", ".yml")):
                    continue
                path = os.path.join(root, filename)
                rel = os.path.relpath(path, text)
                if rel.count(os.sep) > 2:
                    continue
                try:
                    with open(path, encoding="utf-8", errors="replace") as handle:
                        files[rel] = handle.read()
                except OSError as exc:
                    raise ValidationError(f"could not read {rel}: {exc}") from exc
        if not files:
            raise ValidationError(f"no agent files found in {text}",
                                  looked_for="*.md, *.json, *.yaml")
        return files
    if os.path.isfile(text):
        with open(text, encoding="utf-8", errors="replace") as handle:
            body = handle.read()
        stripped = body.lstrip()
        if stripped.startswith(("{", "[")) and text.lower().endswith(".json"):
            try:
                return json.loads(body)
            except ValueError:
                return body
        return body
    # a string that names a file but has no file behind it is a mistake worth naming. a
    # bare sentence with a full stop is content, which is how a curl pipe gets here.
    if "\n" not in text and len(text) < 400 and (
            os.path.splitext(text)[1].lower() in {".md", ".json", ".yaml", ".yml", ".txt"}
            or text.startswith((".", "~")) or ("/" in text and " " not in text)):
        raise ValidationError(f"no such file or directory: {text}")
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def _hint_from(ref: str) -> str:
    if not isinstance(ref, str):
        return ""
    name = os.path.basename(ref.strip().rstrip("/"))
    return os.path.splitext(name)[0] if name not in {".", ""} else ""


def detect(payload: Any) -> str:
    """name the format before reading it, so an unknown file says so instead of
    producing an agent with an empty prompt."""
    if isinstance(payload, dict):
        if str(payload.get("kind") or "").lower() == "agent" and "instructions" in payload:
            return SOURCE_CRITTERTOWN
        nested = payload.get("agents") or payload.get("crews") or payload.get("agents_config")
        if isinstance(nested, dict) and nested:
            sample = next((value for value in nested.values() if isinstance(value, dict)), None)
            if sample and any(key in sample for key in ("role", "goal", "backstory", "tools")):
                return SOURCE_CREWAI
        if isinstance(nested, list) and nested and any(
                key in nested[0] for key in ("role", "goal", "backstory")):
            return SOURCE_CREWAI
        if "role" in payload and ("goal" in payload or "backstory" in payload):
            return SOURCE_CREWAI
        if payload.get("role") and isinstance(payload.get("agents"), (dict, list)):
            return SOURCE_CREWAI
        if "instructions" in payload and any(
                key in payload for key in ("model", "tools", "object")):
            return SOURCE_OPENAI
        if any(isinstance(value, str) for value in payload.values()) and any(
                os.path.basename(str(key)).upper() in {"AGENTS.MD", "SOUL.MD", "IDENTITY.MD"}
                for key in payload):
            return SOURCE_OPENCLAW
        if payload.get("skills") is not None and ("capabilities" in payload or "url" in payload):
            return SOURCE_A2A
        if payload.get("name") and any(key in payload for key in ("goal", "backstory", "llm")):
            return SOURCE_CREWAI
        raise ValidationError("could not tell which format this json is",
                              keys=sorted(payload)[:20], formats=sorted(AGENT_SOURCES))
    if isinstance(payload, str):
        stripped = payload.lstrip()
        if stripped.startswith("{"):
            try:
                return detect(json.loads(payload))
            except ValueError:
                pass
        if payload.startswith("---"):
            return SOURCE_CLAUDE_CODE
        raise ValidationError(
            "this looks like prose with no frontmatter, so there is no name or tools to "
            "trust. add a `---` block, or pass source= explicitly with a name")
    raise ValidationError(f"cannot import an agent from {type(payload).__name__}")


def import_agent(ref: Any, source: str = "", fetcher=None, agent_name: str = "") -> AgentManifest:
    payload = read_source(ref, fetcher=fetcher) if isinstance(ref, str) else ref
    chosen = (source or "").strip().lower()
    if not chosen:
        chosen = detect(payload)
    hint = _hint_from(ref) if isinstance(ref, str) else ""
    if chosen == SOURCE_CLAUDE_CODE:
        if isinstance(payload, dict):
            raise ValidationError("a claude-code subagent is a markdown file, not a json object")
        return from_claude_code(payload, name_hint=hint)
    if chosen == SOURCE_OPENCLAW:
        if not isinstance(payload, dict):
            raise ValidationError("an openclaw style agent is a bundle of files, not one blob")
        return from_openclaw(payload, name_hint=hint)
    if chosen == SOURCE_CREWAI:
        return from_crewai(payload, agent_name=agent_name)
    if chosen == SOURCE_A2A:
        return from_a2a(payload)
    if chosen == SOURCE_OPENAI:
        return from_openai(payload)
    if chosen == SOURCE_CRITTERTOWN:
        return from_crittertown(payload)
    raise ValidationError(f"unknown agent source '{chosen}'", formats=sorted(AGENT_SOURCES))


# ---- export ------------------------------------------------------------
def export_agent(agent: dict, profile: dict | None = None) -> dict:
    """the manifest of an agent that already lives here, so adopting it elsewhere is
    the same operation as importing it."""
    profile = profile or {}
    namespaces = profile.get("namespaces") or {}
    knowledge = [
        {"title": key, "text": str((entry or {}).get("text") or entry)}
        for key, entry in (namespaces.get("knowledge") or {}).items()
    ]
    memory = {key: (entry or {}) for key, entry in (namespaces.get("memory") or {}).items()}
    scope = (agent.get("scope") or {})
    return AgentManifest(
        name=agent["name"],
        role=agent.get("role") or "",
        instructions=agent.get("system_prompt") or "",
        model=agent.get("model") or "",
        skills=_as_list(agent.get("skills")),
        tools=_as_list(agent.get("tools")),
        knowledge=knowledge,
        memory=memory,
        approval={"permissions": dict(scope.get("permissions") or {})},
        runtime=dict(agent.get("runtime") or {}),
        channels=_as_list(scope.get("channels")),
        metadata={"origin": "crittertown", "agent_id": agent["id"],
                  "team_id": agent["team_id"], "status": agent.get("status", "")},
        source=SOURCE_CRITTERTOWN,
    ).to_dict()


# ---- adoption ----------------------------------------------------------
PERMISSION_MAP = {"can_delegate": "send_a2a", "delegate": "send_a2a",
                  "spend": "spend", "approve_workflows": "approve_workflows"}


def adopt(app, team_id: str, manifest: AgentManifest, name: str = "") -> dict:
    """make the imported agent real: a row, a profile, and nothing it did not ask for.

    the imported agent lands idle and unassigned. it does not get a computer, a task or
    a channel because a file mentioned one: those are the crew owner's decisions, and an
    import that grants them silently is a supply chain problem, not a feature.
    """
    app.crews.get_team(team_id)
    taken = {agent["name"].lower() for agent in app.crews.list_agents(team_id)}
    final_name = (name or manifest.name).strip()
    if final_name.lower() in taken:
        raise ValidationError(f"this crew already has an agent called '{final_name}'",
                              hint="pass name= to import it under a different name")
    permissions = {}
    for source_key, target_key in PERMISSION_MAP.items():
        if source_key in (manifest.approval or {}):
            permissions[target_key] = bool(manifest.approval[source_key])
    scope = {"permissions": permissions} if permissions else None
    agent = app.crews.add_agent(
        team_id,
        final_name,
        role=manifest.role or final_name,
        model=manifest.model,
        system_prompt=manifest.instructions,
        skills=manifest.skills,
        tools=manifest.tools,
        runtime={**manifest.runtime, "source": manifest.source} if manifest.runtime else None,
        scope=scope,
    )
    profile = app.profile
    written: list[str] = []
    if profile is not None:
        for index, doc in enumerate(manifest.knowledge or []):
            title = str(doc.get("title") or f"imported doc {index + 1}")[:120]
            profile.set(agent["id"], "knowledge", title,
                        {"text": str(doc.get("text") or "")[:40000],
                         "origin": manifest.source}, origin=f"import:{manifest.source}")
            written.append(f"knowledge.{title}")
        for key, value in (manifest.memory or {}).items():
            profile.set(agent["id"], "memory", str(key), value,
                        origin=f"import:{manifest.source}")
            written.append(f"memory.{key}")
        profile.set(agent["id"], "instructions", "import-source",
                    {"source": manifest.source,
                     "role": manifest.role,
                     "unmapped": manifest.unmapped,
                     "approval": manifest.approval},
                    origin=f"import:{manifest.source}")
        written.append("instructions.import-source")
    warnings = list(manifest.warnings)
    deployed_model = getattr(app.settings, "llm_model", "")
    if manifest.model and deployed_model and manifest.model != deployed_model:
        warnings.append(
            f"the source says model '{manifest.model}', and this deployment answers with "
            f"'{deployed_model}'; the agent carries the name, the deployment decides")
    if manifest.channels:
        warnings.append("channel bindings were not applied: a channel is a crew decision")
    for item in manifest.unmapped:
        warnings.append(f"not imported: {item}")
    if app.audit is not None:
        app.audit.record_event("agent.imported", {
            "team_id": team_id, "agent_id": agent["id"], "source": manifest.source,
            "name": final_name, "warnings": len(warnings)})
    return {"agent": agent, "profile_entries": written, "warnings": warnings,
            "manifest": manifest.to_dict(),
            "imported_from": manifest.source}
