"""the boundary an ingredient exists behind.

an ingredient is the platform's. an agent vm is built from the agent's own harness, so the
test reads the dockerfile's agent stage and checks what it actually copies: no ingredient,
no toolbox, and nothing a copied file imports from either.
"""

import importlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
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


def platform_stage() -> str:
    text = DOCKERFILE.read_text()
    start = text.find("as platform")
    end = text.find("as agent-vm")
    return text[start:end] if start != -1 and end != -1 else ""


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

    def test_the_platform_stage_copies_every_package_it_imports(self):
        """a missing package is an ImportError at the first request, not at build time:
        `pip install -e .` is happy with a source tree the image never copied."""
        stage = platform_stage()
        packages = sorted(
            path.name
            for path in ROOT.iterdir()
            if path.is_dir() and (path / "__init__.py").exists()
        )
        self.assertTrue(packages, "found no packages at the root to check")
        for name in packages:
            self.assertIn(f"COPY {name} ./{name}", stage, f"the image does not copy {name}/")

    def test_the_platform_stage_is_named_and_the_deploy_asks_for_it(self):
        """the agent vm is the last stage, so a bare build produces the harness. every
        service in docker-compose therefore names the platform stage, or the api
        container comes up with no api in it."""
        self.assertIn("as platform", DOCKERFILE.read_text(), "the platform stage needs a name")

        lines = [
            line.strip()
            for line in (ROOT / "docker-compose.yml").read_text().splitlines()
            if not line.strip().startswith("#")
        ]
        self.assertEqual(
            len([line for line in lines if line == "target: platform"]), 3,
            "api, worker and beat all build the platform",
        )
        self.assertEqual(
            [line for line in lines if line == "build: ."], [],
            "build: . builds the agent vm; every service needs a target",
        )

    def test_a_copy_does_not_flatten_the_packages_it_copies(self):
        """`COPY core api ./` copies both directories' contents into the image root, so
        the image builds and starts with no `api` module."""
        for line in DOCKERFILE.read_text().splitlines():
            stripped = line.strip()
            if not stripped.upper().startswith("COPY"):
                continue
            args = [a for a in stripped.split()[1:] if not a.startswith("--")]
            sources, destination = args[:-1], args[-1]
            directories = [s for s in sources if (ROOT / s).is_dir()]
            if directories and destination.rstrip("/") in {".", ""}:
                self.fail(f"flattening copy: {stripped}")

    def test_the_platform_is_the_only_thing_that_names_an_upstream(self):
        # the credits file is the one place a project name may appear
        credits = (ROOT / "crittertown" / "thirdparty.md").read_text().lower()
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


class EntryPointTests(unittest.TestCase):
    """the strings nobody imports until a person runs the command.

    uvicorn takes the app as an import string and the provision plans run `python -m`
    in another shell, so a stale module path is invisible to the suite and fatal to the
    first user. these read the strings the code actually uses and import them.
    """

    def setUp(self):
        # the plan strings are repo-relative module paths, so the root has to be importable
        # even when the suite runs from inside crittertown/tests
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))

    def test_the_cli_serve_target_imports(self):
        from core.cli import SERVE_TARGET

        module, _, factory = SERVE_TARGET.partition(":")
        loaded = importlib.import_module(module)
        self.assertTrue(factory, "the serve target names no factory")
        self.assertTrue(callable(getattr(loaded, factory, None)),
                        f"{module} has no {factory}")

    def test_every_module_a_plan_runs_exists(self):
        pattern = re.compile(r'"-m",\s*"([a-zA-Z_][\w.]*)"')
        files = [ROOT / "sandbox" / "provision.py"] + list(
            (ROOT / "ingredients").rglob("create.py"))
        checked = 0
        for path in files:
            for module in pattern.findall(path.read_text()):
                if module in {"pip", "venv", "json.tool"}:
                    continue
                checked += 1
                try:
                    importlib.import_module(module)
                except ImportError as exc:
                    self.fail(f"{path.relative_to(ROOT)} runs `python -m {module}`, which "
                              f"does not import: {exc}")
        self.assertGreater(checked, 0, "the plan scan found no module to check")


class AgentVmHarnessTests(unittest.TestCase):
    """the agent stage copies files by name, so the suite runs what it copied.

    the boundary tests above prove no *platform* code is in the agent vm. this proves
    the stage still works: copy exactly those files into an empty directory, add the
    bare `core/__init__.py` the dockerfile touches, and import the harness with the
    repo root off the path. a harness file that reaches for a vendored provider client
    passes every boundary test and fails here, which is how it was found in the image.
    """

    def stage_copy(self, base: pathlib.Path) -> None:
        for path in copied_paths(agent_stage()):
            target = base / path.relative_to(ROOT)
            if path.is_dir():
                shutil.copytree(path, target, dirs_exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(path, target)
        (base / "core").mkdir(exist_ok=True)
        (base / "core" / "__init__.py").touch()

    def test_the_copied_harness_imports_without_the_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            self.stage_copy(base)
            code = ("import core.config, core.errors, sandbox.base, sandbox.http, "
                    "agents.runtime; print('harness ok')")
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            env["PYTHONPATH"] = str(base)
            result = subprocess.run([sys.executable, "-c", code], cwd=base, env=env,
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(
                result.returncode, 0,
                "the agent vm cannot start its own harness:\n" + result.stderr[-1500:])

    def test_the_harness_does_not_need_the_vendor_clients(self):
        # the vm asks the platform for compute; only the platform's ingredient adapters
        # talk to a provider, and they ship the vendored clients. if the harness ever
        # imports one, the stage has to copy it and the vm stops being portable.
        imports = re.compile(r"^\s*(?:from|import)\s+vendor\b", re.MULTILINE)
        for path in copied_paths(agent_stage()):
            if path.suffix != ".py":
                continue
            self.assertIsNone(
                imports.search(path.read_text()),
                f"{path.relative_to(ROOT)} imports a vendored provider client")
