"""workflow importers: n8n and dify exports become clawcrew dags.

the contract these tests hold the importers to is the engine's own:
`validate_definition` must accept what the importer produced. an import that
"works" but drafts a dag the engine refuses is a broken import, so every case ends
with that check.
"""

from __future__ import annotations

import json
import unittest

import base  # noqa: F401  (puts the backend on sys.path)

from clawcrew.errors import ValidationError
from clawcrew.orgchart.authoring.importers import (
    SOURCE_DIFY,
    SOURCE_N8N,
    ImportResult,
    from_dify,
    from_n8n,
    import_workflow,
)
from clawcrew.create.workflow.service import validate_definition

N8N_EXPORT = {
    "name": "nightly research digest",
    "nodes": [
        {"name": "Schedule", "type": "n8n-nodes-base.scheduleTrigger", "parameters": {}},
        {
            "name": "Collect Sources",
            "type": "n8n-nodes-base.set",
            "parameters": {"key": "sources", "value": ["hn", "arxiv"]},
        },
        {
            "name": "Fetch Page",
            "type": "n8n-nodes-base.httpRequest",
            "parameters": {"url": "https://news.ycombinator.com", "method": "GET"},
        },
        {
            "name": "Enough?",
            "type": "n8n-nodes-base.if",
            "parameters": {},
        },
        {
            "name": "Write Digest",
            "type": "n8n-nodes-base.openAi",
            "parameters": {"prompt": "summarise the fetched pages"},
        },
    ],
    "connections": {
        "Schedule": {"main": [[{"node": "Collect Sources", "index": 0}]]},
        "Collect Sources": {"main": [[{"node": "Fetch Page", "index": 0}]]},
        "Fetch Page": {"main": [[{"node": "Enough?", "index": 0}]]},
        "Enough?": {"main": [[{"node": "Write Digest", "index": 0}]]},
    },
}

DIFY_EXPORT = {
    "app": {"name": "support triage", "mode": "workflow"},
    "workflow": {
        "graph": {
            "nodes": [
                {"id": "1700000000001", "type": "start", "data": {"title": "Start"}},
                {"id": "1700000000002", "type": "llm", "data": {"title": "Classify", "desc": "sort the ticket"}},
                {
                    "id": "1700000000003",
                    "type": "if-else",
                    "data": {"title": "Urgent?"},
                },
                {
                    "id": "1700000000004",
                    "type": "code",
                    "data": {"title": "Escalate", "code": "print('page someone')"},
                },
                {
                    "id": "1700000000005",
                    "type": "assigner",
                    "data": {"title": "Remember", "key": "urgency", "value": "high"},
                },
            ],
            "edges": [
                {"source": "1700000000001", "target": "1700000000002"},
                {"source": "1700000000002", "target": "1700000000003"},
                {"source": "1700000000003", "target": "1700000000004"},
                {"source": "1700000000004", "target": "1700000000005"},
            ],
        }
    },
}


def by_id(definition: dict) -> dict:
    return {node["id"]: node for node in definition["nodes"]}


class N8NImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = from_n8n(N8N_EXPORT)
        self.nodes = by_id(self.result.definition)

    def test_result_is_an_import_result_with_the_workflow_name(self):
        self.assertIsInstance(self.result, ImportResult)
        self.assertEqual(self.result.name, "nightly research digest")
        self.assertEqual(self.result.definition["source"], "import")

    def test_trigger_nodes_are_dropped_not_translated(self):
        self.assertNotIn("schedule", self.nodes)
        self.assertEqual(len(self.result.definition["nodes"]), 4)

    def test_dependencies_follow_the_connections(self):
        self.assertEqual(self.nodes["collect-sources"].get("depends_on", []), [])
        self.assertEqual(self.nodes["fetch-page"]["depends_on"], ["collect-sources"])
        self.assertEqual(self.nodes["write-digest"]["depends_on"], ["enough"])

    def test_set_becomes_set_context(self):
        node = self.nodes["collect-sources"]
        self.assertEqual(node["type"], "set_context")
        self.assertEqual(node["key"], "sources")

    def test_an_unmappable_node_keeps_its_payload_for_the_human(self):
        node = self.nodes["fetch-page"]
        self.assertEqual(node["type"], "agent_task")
        self.assertIn("https://news.ycombinator.com", node["instruction"])
        self.assertTrue(any("httpRequest" in item for item in self.result.unmapped))
        self.assertTrue(self.result.warnings)

    def test_unresolvable_branch_becomes_a_note_not_a_broken_dag(self):
        node = self.nodes["enough"]
        self.assertEqual(node["type"], "set_context")
        self.assertEqual(node["value"], {"note": "branch point from the imported workflow"})
        self.assertNotIn("targets", node)
        self.assertTrue(any("branches" in warning for warning in self.result.warnings))

    def test_import_validates_against_the_engine(self):
        validate_definition(self.result.definition)  # must not raise

    def test_no_private_helper_keys_leak_into_the_definition(self):
        for node in self.result.definition["nodes"]:
            self.assertNotIn("_needs", node)
            self.assertNotIn("pop_targets", node)

    def test_duplicate_node_names_get_distinct_ids(self):
        payload = {
            "nodes": [
                {"name": "HTTP Request", "type": "n8n-nodes-base.httpRequest", "parameters": {}},
                {"name": "HTTP Request", "type": "n8n-nodes-base.httpRequest", "parameters": {}},
            ],
            "connections": {},
        }
        definition = from_n8n(payload).definition
        ids = [node["id"] for node in definition["nodes"]]
        self.assertEqual(ids, ["http-request", "http-request-2"])
        validate_definition(definition)

    def test_json_text_is_accepted(self):
        result = from_n8n(json.dumps(N8N_EXPORT))
        self.assertEqual(len(result.definition["nodes"]), 4)

    def test_broken_json_is_a_validation_error(self):
        with self.assertRaises(ValidationError):
            from_n8n("{not json")

    def test_a_workflow_with_no_nodes_is_rejected(self):
        with self.assertRaises(ValidationError):
            from_n8n({"nodes": []})

    def test_only_triggers_is_rejected_rather_than_returning_an_empty_dag(self):
        with self.assertRaises(ValidationError):
            from_n8n(
                {
                    "nodes": [{"name": "Start", "type": "n8n-nodes-base.start", "parameters": {}}],
                    "connections": {},
                }
            )


class DifyImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = from_dify(DIFY_EXPORT)
        self.nodes = by_id(self.result.definition)

    def test_name_comes_from_the_app_block(self):
        self.assertEqual(self.result.name, "support triage")

    def test_start_node_is_dropped(self):
        self.assertEqual(len(self.result.definition["nodes"]), 4)

    def test_edges_become_dependencies(self):
        self.assertEqual(self.nodes["classify"].get("depends_on", []), [])
        self.assertEqual(self.nodes["urgent"]["depends_on"], ["classify"])
        self.assertEqual(self.nodes["escalate"]["depends_on"], ["urgent"])
        self.assertEqual(self.nodes["remember"]["depends_on"], ["escalate"])

    def test_llm_node_becomes_an_agent_task_carrying_its_description(self):
        node = self.nodes["classify"]
        self.assertEqual(node["type"], "agent_task")
        self.assertEqual(node["instruction"], "sort the ticket")

    def test_code_node_becomes_an_agent_task_and_is_reported_unmapped(self):
        node = self.nodes["escalate"]
        self.assertEqual(node["type"], "agent_task")
        self.assertIn("print('page someone')", node["instruction"])
        self.assertTrue(any("code" in item for item in self.result.unmapped))

    def test_assigner_becomes_set_context(self):
        node = self.nodes["remember"]
        self.assertEqual(node["type"], "set_context")
        self.assertEqual(node["key"], "urgency")

    def test_conditional_node_keeps_the_dag_valid(self):
        self.assertEqual(self.nodes["urgent"]["type"], "set_context")
        validate_definition(self.result.definition)

    def test_import_validates_against_the_engine(self):
        validate_definition(self.result.definition)

    def test_missing_graph_is_a_validation_error(self):
        with self.assertRaises(ValidationError):
            from_dify({"app": {"name": "x"}})

    def test_yaml_input_is_refused_with_an_install_hint_when_pyyaml_is_absent(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            with self.assertRaises(ValidationError) as ctx:
                from_dify("app:\n  name: support triage\n")
            self.assertIn("pyyaml", str(ctx.exception.details.get("hint", "")))
        else:
            result = from_dify("app:\n  name: support triage\nworkflow:\n  graph:\n    nodes:\n      - id: a\n        type: llm\n        data:\n          title: Classify\n")
            self.assertEqual(result.name, "support triage")


class DispatchTests(unittest.TestCase):
    def test_source_dispatch(self):
        self.assertEqual(import_workflow(SOURCE_N8N, N8N_EXPORT).name, "nightly research digest")
        self.assertEqual(import_workflow("DIFY", DIFY_EXPORT).name, "support triage")

    def test_unknown_source_lists_the_supported_ones(self):
        with self.assertRaises(ValidationError) as ctx:
            import_workflow("zapier", {})
        self.assertEqual(ctx.exception.details.get("allowed"), [SOURCE_DIFY, SOURCE_N8N])


if __name__ == "__main__":
    unittest.main()
