"""plans: the shape every provisioning path uses, so nothing creates before it prints.

a plan is a list of steps you can read: write this file, run this command. `apply` is a
separate, explicit act. both machines and coordination runtimes go through this, so
"what will this do" has one answer regardless of target.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Step:
    action: str  # exec | write
    description: str
    command: list[str] = field(default_factory=list)
    path: str = ""
    content: str = ""

    def to_dict(self) -> dict:
        return {"action": self.action, "description": self.description,
                "command": self.command, "path": self.path}


@dataclass
class Plan:
    target: str
    kind: str
    name: str
    steps: list[Step]
    facts: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    state_dir: str = ""

    def to_dict(self) -> dict:
        return {
            "target": self.target, "kind": self.kind, "name": self.name,
            "steps": [s.to_dict() for s in self.steps],
            "facts": self.facts, "notes": self.notes, "state_dir": self.state_dir,
            "requires_apply": True,
        }


def run_plan(
    plan: Plan,
    runner: Callable[[list[str]], dict],
    dry_run: bool = True,
    label: str = "",
) -> dict:
    """run the plan, stopping at the first failure. writes land under state_dir."""
    if dry_run:
        return {"applied": False, "plan": plan.to_dict(),
                "reason": "dry run: nothing was created and nothing was charged"}
    results = []
    for step in plan.steps:
        if step.action == "write":
            path = os.path.join(plan.state_dir, step.path)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(step.content)
            results.append({"step": step.description, "wrote": path, "ok": True})
            continue
        outcome = runner(step.command)
        results.append({"step": step.description, **outcome})
        if not outcome.get("ok"):
            break
    ok = all(r.get("ok") for r in results) and len(results) == len(plan.steps)
    return {"applied": True, "ok": ok, "target": plan.target, "kind": plan.kind,
            "name": plan.name, "label": label or plan.kind, "results": results,
            "notes": plan.notes}


def record_plan(outcome: dict, out_dir: str, filename: str) -> str:
    """keep what happened next to the plan, so a machine has a paper trail."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(outcome, indent=2))
    return path
