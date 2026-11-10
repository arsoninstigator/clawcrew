"""workflow importers.

taking the *format*, not the code. n8n and dify are both source-available, not
open source (n8n: Sustainable Use License; dify: Dify Open Source License with
additional conditions), so copying their implementation into an Apache-2.0 project
is not on the table. importing their exported workflow json is: a file format is a
contract, and interoperating with it is exactly what "no lock-in" means.

n8n export:
    {"name": "...", "nodes": [{"name", "type", "parameters"}],
     "connections": {"Node A": {"main": [[{"node": "Node B", "index": 0}]]}}}

dify export:
    {"app": {"name": ..., "mode": "workflow"},
     "workflow": {"graph": {"nodes": [{"id", "data": {"type", "title"}}],
                            "edges": [{"source", "target"}]}}}

both are lossy in different ways, so every import returns warnings instead of
pretending the translation was faithful. unsupported nodes become `agent_task`
nodes carrying their original description: an agent can do a httpRequest, and the
human editing the draft can replace it with a `shell` node if they prefer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from clawcrew.errors import ValidationError

SOURCE_N8N = "n8n"
SOURCE_DIFY = "dify"
SOURCES = {SOURCE_N8N, SOURCE_DIFY}


@dataclass
class ImportResult:
    name: str
    definition: dict
    warnings: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "definition": self.definition,
            "warnings": self.warnings,
            "unmapped": self.unmapped,
        }


def _slug(text: str) -> str:
    keep = "".join(ch if ch.isalnum() else "-" for ch in text.lower()).strip("-")
    while "--" in keep:
        keep = keep.replace("--", "-")
    return keep or "node"


def _dedupe(node_id: str, used: set[str]) -> str:
    candidate = node_id
    index = 2
    while candidate in used:
        candidate = f"{node_id}-{index}"
        index += 1
    used.add(candidate)
    return candidate


# ---- n8n ---------------------------------------------------------------
N8N_TRIGGERS = {"n8n-nodes-base.start", "n8n-nodes-base.manualTrigger", "n8n-nodes-base.scheduleTrigger",
                "n8n-nodes-base.webhook", "n8n-nodes-base.cron"}
N8N_IF = {"n8n-nodes-base.if", "n8n-nodes-base.switch", "n8n-nodes-base.filter"}
N8N_SET = {"n8n-nodes-base.set", "n8n-nodes-base.setNode", "n8n-nodes-base.editFields"}


def from_n8n(payload: Any) -> ImportResult:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise ValidationError("n8n import expects workflow json") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
        raise ValidationError("n8n workflow needs a nodes array")

    warnings: list[str] = []
    unmapped: list[str] = []
    used: set[str] = set()
    by_source_name: dict[str, str] = {}
    drafted: list[dict] = []

    for node in payload["nodes"]:
        if not isinstance(node, dict):
            continue
        source_name = str(node.get("name") or node.get("id") or "node")
        node_type = str(node.get("type") or "")
        if node_type in N8N_TRIGGERS:
            by_source_name[source_name] = ""
            continue
        node_id = _dedupe(_slug(source_name), used)
        by_source_name[source_name] = node_id
        params = node.get("parameters") or {}
        if node_type in N8N_IF:
            drafted.append(
                {
                    "id": node_id,
                    "type": "branch",
                    "targets": [],
                    "cases": [],
                    "_needs": "branch targets and conditions must be set after import",
                }
            )
            warnings.append(f"'{source_name}' was an if/switch node; re-attach its branches by hand")
        elif node_type in N8N_SET:
            key = _slug(str(params.get("key") or source_name))
            value = params.get("value") or params.get("values") or {}
            drafted.append({"id": node_id, "type": "set_context", "key": key, "value": value})
        else:
            instruction = _n8n_instruction(node_type, params)
            drafted.append(
                {
                    "id": node_id,
                    "type": "agent_task",
                    "title": source_name[:200],
                    "instruction": instruction[:2000],
                }
            )
            unmapped.append(f"{node_type or 'unknown'}: {source_name}")
            warnings.append(f"'{source_name}' ({node_type}) became an agent_task, not an equivalent node")

    # dependencies from connections
    connections = payload.get("connections") or {}
    for source, spec in connections.items():
        source_id = by_source_name.get(source)
        if not source_id:
            continue
        for outputs in (spec or {}).get("main") or []:
            for link in outputs or []:
                if not isinstance(link, dict):
                    continue
                target_id = by_source_name.get(str(link.get("node")))
                if target_id:
                    _add_dep(drafted, target_id, source_id)

    definition = _finish(drafted, warnings)
    return ImportResult(
        name=str(payload.get("name") or "imported n8n workflow"),
        definition=definition,
        warnings=warnings,
        unmapped=unmapped,
    )


def _n8n_instruction(node_type: str, params: dict) -> str:
    bits = [f"perform the '{node_type}' step"]
    for key in ("url", "method", "operation", "resource", "functionCode", "jsCode", "text", "prompt"):
        if params.get(key):
            value = str(params[key]).replace("\n", " ")
            bits.append(f"{key}={value[:300]}")
    return "; ".join(bits)


# ---- dify --------------------------------------------------------------
DIFY_TRIGGER_TYPES = {"start", "trigger-webhook", "trigger-schedule", "datasource"}
DIFY_IF = {"if-else", "question-classifier"}
DIFY_CODE = {"code", "template-transform", "http-request"}


def from_dify(payload: Any) -> ImportResult:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = _parse_yaml(payload)
    if not isinstance(payload, dict):
        raise ValidationError("dify import expects a workflow export")

    workflow = payload.get("workflow") or payload.get("graph") or {}
    graph = workflow.get("graph") or workflow
    nodes = graph.get("nodes")
    edges = graph.get("edges") or []
    if not isinstance(nodes, list):
        raise ValidationError("dify export needs workflow.graph.nodes")

    warnings: list[str] = []
    unmapped: list[str] = []
    used: set[str] = set()
    drafted: list[dict] = []
    by_source_id: dict[str, str] = {}

    for node in nodes:
        if not isinstance(node, dict):
            continue
        source_id = str(node.get("id") or "")
        data = node.get("data") or {}
        node_type = str(node.get("type") or data.get("type") or "")
        source_label = str(data.get("title") or node_type or source_id)
        if node_type in DIFY_TRIGGER_TYPES or source_id.endswith("-start"):
            by_source_id[source_id] = ""
            continue
        node_id = _dedupe(_slug(source_label), used)
        by_source_id[source_id] = node_id
        if node_type in DIFY_IF:
            drafted.append({"id": node_id, "type": "branch", "targets": [], "cases": []})
            warnings.append(f"'{source_label}' was a conditional node; re-attach its branches by hand")
        elif node_type in DIFY_CODE:
            drafted.append(
                {
                    "id": node_id,
                    "type": "agent_task",
                    "title": source_label[:200],
                    "instruction": f"perform the dify '{node_type}' step: "
                    + json.dumps(data, sort_keys=True)[:1500],
                }
            )
            unmapped.append(f"{node_type}: {source_label}")
        elif node_type == "assigner" or node_type == "variable-assigner":
            key = _slug(str(data.get("key") or source_label))
            drafted.append({"id": node_id, "type": "set_context", "key": key, "value": data.get("value")})
        else:
            drafted.append(
                {
                    "id": node_id,
                    "type": "agent_task",
                    "title": source_label[:200],
                    "instruction": str(data.get("desc") or data.get("instruction") or source_label)[:2000],
                }
            )
            warnings.append(f"'{source_label}' ({node_type or 'unknown'}) became an agent_task")

    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source_id = by_source_id.get(str(edge.get("source")))
        target_id = by_source_id.get(str(edge.get("target")))
        if source_id and target_id:
            _add_dep(drafted, target_id, source_id)

    definition = _finish(drafted, warnings)
    app = payload.get("app") or {}
    return ImportResult(
        name=str(app.get("name") or payload.get("name") or "imported dify workflow"),
        definition=definition,
        warnings=warnings,
        unmapped=unmapped,
    )


def _parse_yaml(text: str) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ValidationError(
            "dify exports yaml; install pyyaml or convert the export to json",
            hint="pip install pyyaml",
        ) from exc
    try:
        return yaml.safe_load(text) or {}
    except Exception as exc:  # noqa: BLE001 - yaml raises its own types
        raise ValidationError(f"could not parse the dify yaml export: {exc}") from exc


# ---- shared ------------------------------------------------------------
def _add_dep(nodes: list[dict], node_id: str, dep: str) -> None:
    for node in nodes:
        if node["id"] == node_id:
            deps = node.setdefault("depends_on", [])
            if dep not in deps:
                deps.append(dep)
            return


def _finish(nodes: list[dict], warnings: list[str]) -> dict:
    """strip helper keys and repair branch targets that lost their edges."""
    ids = [node["id"] for node in nodes]
    if not ids:
        raise ValidationError("nothing importable was found in this workflow export")
    for node in nodes:
        node.pop("_needs", None)
        if node["type"] == "branch" and not node.get("targets"):
            # a branch with no targets would fail validation; drop it and keep its deps
            warnings.append(f"dropped branch '{node['id']}' because its branches were not resolvable")
            node["pop_targets"] = True
            node["type"] = "set_context"
            node["key"] = f"{node['id']}-branch"
            node["value"] = {"note": "branch point from the imported workflow"}
    for node in nodes:
        if node.pop("pop_targets", False):
            node.pop("targets", None)
    definition = {"nodes": nodes, "source": "import"}
    if not definition["nodes"]:
        raise ValidationError("nothing importable was found in this workflow export")
    return definition


def import_workflow(source: str, payload: Any) -> ImportResult:
    source = (source or "").strip().lower()
    if source == SOURCE_N8N:
        return from_n8n(payload)
    if source == SOURCE_DIFY:
        return from_dify(payload)
    raise ValidationError(f"unknown workflow source '{source}'", allowed=sorted(SOURCES))
