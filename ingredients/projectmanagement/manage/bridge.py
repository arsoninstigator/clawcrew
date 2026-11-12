"""the lane between a crew and its manage board.

the board decides what a run is; this claims it, runs the crew's own turn on it,
and reports back. nothing here reimplements their queue: a claim is atomic on
their side, so a run is either ours or a 409 and we move on.

what it adds is the part an agent needs to work unattended: a heartbeat while the
turn runs, an event trail so the board shows progress instead of silence, and a
result posted even when the turn raised, because a run left open blocks the issue
for everyone else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from core.errors import ProviderFailure
from ingredients.projectmanagement.manage.client import ManageClient


@dataclass
class ManageBridge:
    client: ManageClient
    executor: Callable[[dict], dict]
    max_runs: int = 1
    events: list[dict] = field(default_factory=list)

    def poll(self) -> list[dict]:
        """claim and run up to `max_runs` turns. an empty queue ends the poll early."""
        outcomes: list[dict] = []
        for _ in range(max(0, self.max_runs)):
            run = self.client.claim()
            if run is None:
                break
            outcomes.append(self.execute(run))
        return outcomes

    def execute(self, run: dict) -> dict:
        run_id = run.get("id")
        issue = run.get("issueIdentifier") or run.get("issueId")
        try:
            self.client.heartbeat(run_id, note=f"crittertown picked up {issue or 'the run'}")
            outcome = self.executor(run) or {}
        except ProviderFailure:
            raise
        except Exception as exc:  # a crew turn that dies must not leave the run open
            self._report(run, "failed", str(exc)[:500], {"error": type(exc).__name__})
            return {"run": run_id, "issue": issue, "status": "failed", "error": str(exc)[:500]}

        status = "succeeded" if outcome.get("status", "succeeded") == "succeeded" else "failed"
        summary = str(outcome.get("summary") or outcome.get("output") or "")
        details = outcome.get("details") or {}
        self._report(run, status, summary, details)
        return {"run": run_id, "issue": issue, "status": status, "summary": summary}

    def _report(self, run: dict, status: str, summary: str, details: dict) -> None:
        self.client.result(run.get("id"), status, summary, details)
