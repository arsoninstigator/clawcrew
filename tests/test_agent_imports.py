"""agents are importable: the format is read, one manifest is produced, and only then
does anything get created."""

from __future__ import annotations

import json
import os
import unittest

from base import AppCase

from clawcrew.agent_imports import (
    AGENT_SOURCES,
    import_agent,
    adopt,
    export_agent,
    read_source,
)
from clawcrew.errors import ValidationError

CLAUDE_STYLE = """---
name: triage
description: sorts incoming support mail
tools: [shell, http]
model: claude-sonnet-4
permission-mode: acceptEdits
---
you triage support mail. tag it, route it, and escalate only when a human must decide.
"""

LISTY = """---
name: reviewer
description: reviews patches
tools:
  - shell
  - http
---
review the patch, then say what you would block on.
"""

OPENCLAW_FILES = {
    "AGENTS.md": "# operating rules\nanswer in the owner's words, never invent a fact.\n",
    "SOUL.md": "## persona\npatient, precise, allergic to filler.\n",
    "IDENTITY.md": "---\nname: desk\nrole: support\ntimezone: asia/calcutta\n---\n",
    "USER.md": "the owner runs a small studio and hates being called a company.\n",
    "skills/report/SKILL.md": "# report\nwrite the weekly report.\n",
}

CREWAI = {"agents": {"triage": {
    "role": "support triage",
    "goal": "every mail is tagged and routed",
    "backstory": "ten years of support queues",
    "tools": ["shell"],
    "llm": "gpt-4.1",
    "allow_delegation": False,
    "max_iter": 12,
}}}

A2A_CARD = {
    "name": "invoice clerk",
    "description": "reads invoices and files them",
    "url": "https://clerks.example/a2a",
    "version": "1.2.0",
    "capabilities": {"streaming": False},
    "skills": [{"id": "read-invoice", "name": "read invoice",
                "description": "turn a pdf into line items",
                "examples": ["read this invoice"]}],
    "securitySchemes": {"bearer": {"type": "http"}},
}

ASSISTANT = {
    "id": "asst_1",
    "object": "assistant",
    "name": "inbox zero",
    "instructions": "keep the inbox at zero, decide fast, escalate rarely.",
    "model": "gpt-4.1-mini",
    "tools": [{"type": "function", "function": {"name": "archive"}},
              {"type": "file_search"}],
    "tool_resources": {"file_search": {"vector_store_ids": ["vs_1"]}},
}


class FormatTests(AppCase):
    def test_a_claude_code_subagent_becomes_a_manifest(self):
        manifest = import_agent(CLAUDE_STYLE)
        self.assertEqual(manifest.source, "claude-code")
        self.assertEqual(manifest.name, "triage")
        self.assertEqual(manifest.tools, ["shell", "http"])
        self.assertEqual(manifest.model, "claude-sonnet-4")
        self.assertTrue(manifest.instructions.startswith("you triage support mail"))
        self.assertIn("frontmatter.permission-mode", manifest.unmapped)

    def test_a_yaml_list_in_the_frontmatter_is_read_as_a_list(self):
        manifest = import_agent(LISTY)
        self.assertEqual(manifest.tools, ["shell", "http"])

    def test_prose_with_no_frontmatter_is_refused_rather_than_guessed(self):
        with self.assertRaises(ValidationError):
            import_agent("you are a helpful agent, please do good work")

    def test_a_directory_of_markdown_is_one_agent(self):
        manifest = import_agent(OPENCLAW_FILES)
        self.assertEqual(manifest.source, "openclaw")
        self.assertEqual(manifest.name, "desk")
        self.assertEqual(manifest.role, "support")
        self.assertIn("operating rules", manifest.instructions)
        self.assertIn("persona", manifest.instructions)
        self.assertEqual(manifest.skills, ["report"])
        titles = [doc["title"] for doc in manifest.knowledge]
        self.assertTrue(any("about-the-user" in title for title in titles))
        self.assertIn("identity.timezone", manifest.unmapped)

    def test_a_crewai_entry_keeps_its_goal_and_backstory(self):
        manifest = import_agent(CREWAI)
        self.assertEqual(manifest.source, "crewai")
        self.assertEqual(manifest.name, "triage")
        self.assertIn("backstory", manifest.instructions)
        self.assertIn("goal", manifest.instructions)
        self.assertFalse(manifest.approval["can_delegate"])
        self.assertIn("max_iter", manifest.unmapped)

    def test_an_a2a_card_imports_without_pretending_it_has_a_prompt(self):
        manifest = import_agent(A2A_CARD)
        self.assertEqual(manifest.name, "invoice clerk")
        self.assertEqual(manifest.skills, ["read-invoice"])
        self.assertTrue(any("agent card" in warning for warning in manifest.warnings))
        self.assertIn("card.url", manifest.unmapped)
        self.assertTrue(manifest.knowledge)

    def test_an_assistant_object_keeps_instructions_and_named_functions(self):
        manifest = import_agent(ASSISTANT)
        self.assertEqual(manifest.name, "inbox zero")
        self.assertEqual(manifest.tools, ["archive"])
        self.assertIn("tool.file_search", manifest.unmapped)
        self.assertEqual(manifest.metadata["id"], "asst_1")

    def test_unknown_json_says_which_formats_it_could_read(self):
        with self.assertRaises(ValidationError) as caught:
            import_agent({"something": "else"})
        self.assertIn("formats", caught.exception.details)

    def test_a_manifest_is_also_an_input(self):
        manifest = import_agent({"kind": "agent", "name": "portable",
                                 "instructions": "be brief", "skills": ["writing"]})
        self.assertEqual(manifest.source, "clawcrew")
        self.assertEqual(manifest.skills, ["writing"])

    def test_a_url_is_read_through_the_same_path_as_a_file(self):
        manifest = import_agent("https://example.invalid/agents/triage.md",
                                fetcher=lambda url: CLAUDE_STYLE)
        self.assertEqual(manifest.name, "triage")

    def test_every_format_is_listed_with_a_description(self):
        self.assertEqual(set(AGENT_SOURCES), {"claude-code", "openclaw", "crewai", "a2a",
                                             "openai-assistant", "clawcrew"})
        self.assertTrue(all(AGENT_SOURCES.values()))

    def test_a_file_on_disk_is_read_from_its_path(self):
        path = os.path.join(self.tmp, "reviewer.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(LISTY)
        self.assertEqual(import_agent(path).name, "reviewer")
        with self.assertRaises(ValidationError):
            read_source(os.path.join(self.tmp, "nope.md"))


class AdoptionTests(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew()

    def test_an_imported_agent_lands_idle_and_unassigned(self):
        outcome = adopt(self.app, self.team["id"], import_agent(CLAUDE_STYLE))
        agent = outcome["agent"]
        self.assertEqual(agent["team_id"], self.team["id"])
        self.assertEqual(agent["status"], "idle")
        self.assertEqual(agent["tools"], ["shell", "http"])
        self.assertEqual(agent["system_prompt"].strip().splitlines()[0],
                         "you triage support mail. tag it, route it, and escalate only "
                         "when a human must decide.")
        self.assertEqual(self.kanban.list_tasks(self.team["id"]), [])
        self.assertIsNone(self.crews.get_computer(agent["id"]))

    def test_the_profile_carries_the_documents_and_the_approval_policy(self):
        manifest = import_agent(CLAUDE_STYLE)
        manifest.knowledge.append({"title": "style guide", "text": "short sentences"})
        outcome = adopt(self.app, self.team["id"], manifest)
        profile = self.app.profile.profile(outcome["agent"]["id"])
        self.assertIn("style guide", profile["namespaces"]["knowledge"])
        self.assertEqual(profile["namespaces"]["instructions"]["import-source"]["source"],
                         "claude-code")

    def test_a_delegation_flag_from_the_source_lands_in_permissions(self):
        agent = adopt(self.app, self.team["id"], import_agent(CREWAI))["agent"]
        self.assertFalse(agent["scope"]["permissions"]["send_a2a"])
        self.assertFalse(agent["scope"]["permissions"]["spend"])

    def test_two_agents_cannot_share_a_name_and_a_rename_is_offered(self):
        adopt(self.app, self.team["id"], import_agent(CLAUDE_STYLE))
        with self.assertRaises(ValidationError) as caught:
            adopt(self.app, self.team["id"], import_agent(CLAUDE_STYLE))
        self.assertIn("already has an agent", caught.exception.message)
        renamed = adopt(self.app, self.team["id"], import_agent(CLAUDE_STYLE), name="triage two")
        self.assertEqual(renamed["agent"]["name"], "triage two")

    def test_a_deployment_that_speaks_another_model_says_so(self):
        outcome = adopt(self.app, self.team["id"], import_agent(CLAUDE_STYLE))
        self.assertTrue(any("deployment answers with" in warning
                            for warning in outcome["warnings"]))

    def test_export_and_import_round_trip(self):
        adopted = adopt(self.app, self.team["id"], import_agent(CLAUDE_STYLE))["agent"]
        profile = self.app.profile.profile(adopted["id"])
        manifest = export_agent(adopted, profile)
        again = adopt(self.app, self.team["id"], import_agent(manifest), name="triage again")
        self.assertEqual(again["agent"]["system_prompt"], adopted["system_prompt"])
        self.assertEqual(again["agent"]["tools"], adopted["tools"])
        self.assertEqual(manifest["kind"], "agent")

    def test_an_imported_agent_can_actually_be_dispatched(self):
        agent = adopt(self.app, self.team["id"], import_agent(CLAUDE_STYLE))["agent"]
        task = self.kanban.create_task(team_id=self.team["id"], title="sort the queue",
                                       column="ready", assignee_agent_id=agent["id"])
        outcome = self.runner.dispatch(task["id"], max_turns=1)
        self.assertTrue(outcome["run_id"])
        prompt_seen = outcome["steps"] or []
        self.assertIsInstance(prompt_seen, list)


if __name__ == "__main__":
    unittest.main()
