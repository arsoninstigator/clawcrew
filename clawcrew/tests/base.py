"""shared fixtures: a fresh app per test, no network, no sleeps."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.app import build_app  # noqa: E402
from core.config import Settings  # noqa: E402


class AppCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="clawcrew-test-")
        self.settings = Settings(
            database_path=os.path.join(self.tmp, "test.db"),
            allow_local_fallback=True,
            default_review_mode="reviewed",
            max_parallel_runs=2,
        )
        self.app = build_app(self.settings)
        self.crews = self.app.crews
        self.kanban = self.app.kanban
        self.a2a = self.app.a2a
        self.runner = self.app.runner

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers -----------------------------------------------------------
    def crew(self, name: str = "growth", **kwargs):
        return self.crews.create_team(name, **kwargs)

    def agent(self, team_id: str, name: str = "researcher", **kwargs):
        kwargs.setdefault("role", "research")
        kwargs.setdefault("skills", ["research"])
        return self.crews.add_agent(team_id, name, **kwargs)

    def task(self, team_id: str, title: str = "do the thing", **kwargs):
        kwargs.setdefault("column", "ready")
        return self.kanban.create_task(team_id, title, **kwargs)
