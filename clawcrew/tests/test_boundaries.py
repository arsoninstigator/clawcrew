"""the boundary an ingredient exists behind.

an ingredient is the platform's. an agent vm is built from the agent's own harness, so the
test reads the dockerfile's agent stage and checks what it actually copies: no ingredient,
no toolbox, and nothing a copied file imports from either.
"""

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"

PLATFORM = ("ingredients", "toolbox", "workbench", "infrastructure")


def agent_stage() -> str:
    text = DOCKERFILE.read_text()
    marker = "as agent-vm"
    start = text.find(marker)
    if start == -1:
        return ""
    return text[start:]


def copied_paths(stage: str) -> list[pathlib.Path]:
    out = []
    for line in stage.splitlines():
        line = line.strip()
        if not line.startswith("COPY "):
            continue
        args = [a for a in line.split()[1:] if not a.startswith("--")]
        for src in args[:-1]:
            out.append(ROOT / src)
    return out


class IngredientBoundaryTests(unittest.TestCase):
    def test_the_agent_stage_does_not_copy_an_ingredient(self):
        stage = agent_stage()
        self.assertTrue(stage, "the dockerfile has no agent-vm stage")
        for path in copied_paths(stage):
            self.assertFalse(
                any(part in PLATFORM for part in path.parts),
                f"the agent vm copies {path.relative_to(ROOT)}, which belongs to the platform",
            )

    def test_nothing_the_agent_vm_copies_imports_the_platform(self):
        stage = agent_stage()
        for path in copied_paths(stage):
            files = [path] if path.is_file() else list(path.rglob("*.py"))
            for file in files:
                if file.suffix != ".py":
                    continue
                text = file.read_text()
                for banned in PLATFORM:
                    self.assertNotIn(
                        f"from {banned}", text,
                        f"{file.relative_to(ROOT)} imports {banned}, which is platform only",
                    )
                    self.assertNotIn(
                        f"import {banned}", text,
                        f"{file.relative_to(ROOT)} imports {banned}, which is platform only",
                    )

    def test_every_ingredient_the_platform_carries_has_a_home(self):
        expected = [
            "ingredients/machines/persistent",
            "ingredients/machines/ephemeral",
            "ingredients/machines/hosted",
            "ingredients/browser",
            "ingredients/schedule",
            "ingredients/durableobjects",
            "ingredients/workflows",
            "ingredients/projectmanagement/manage",
            "ingredients/projectmanagement/kanban",
            "ingredients/credentials/credential_broker",
        ]
        for rel in expected:
            self.assertTrue((ROOT / rel).is_dir(), f"{rel} is missing")
            self.assertTrue((ROOT / rel / "__init__.py").exists() or list((ROOT / rel).glob("*.py")),
                            f"{rel} has no python in it")

    def test_the_platform_is_the_only_thing_that_names_an_upstream(self):
        # the credits file is the one place a project name may appear
        credits = (ROOT / "clawcrew" / "thirdparty.md").read_text().lower()
        for name in ("celesto", "e2b", "langflow", "itsaplan", "infisical", "durable-actors", "kernel-images"):
            self.assertIn(name, credits, f"{name} should be credited")
        for path in ROOT.rglob("*.py"):
            if any(p in {"vendor", ".git", "__pycache__"} for p in path.parts):
                continue
            if path.name == pathlib.Path(__file__).name:
                continue  # this file has to spell the names it forbids
            text = path.read_text().lower()
            for name in ("celesto", "langflow", "itsaplan", "infisical"):
                self.assertNotIn(name, text, f"{path.relative_to(ROOT)} names {name}; rename it in house")


if __name__ == "__main__":
    unittest.main()
