"""provisioning: a plan you can read before anything is created or charged."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from base import AppCase

from clawcrew.sandbox.provision import DISPOSABLE, PERSISTENT, Provisioner
from clawcrew.errors import ValidationError


class FakeRunner:
    def __init__(self, fail_on: str = ""):
        self.commands: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, command: list[str]) -> dict:
        self.commands.append(command)
        ok = not (self.fail_on and self.fail_on in " ".join(command))
        return {"command": command, "ok": ok, "stdout": "ok" if ok else "",
                "stderr": "" if ok else "boom"}


def which_factory(present: tuple[str, ...]):
    return lambda name: f"/usr/bin/{name}" if name in present else None


class ProvisionTests(AppCase):
    def build(self, present: tuple[str, ...] = (), fail_on: str = "") -> Provisioner:
        self.runner = FakeRunner(fail_on=fail_on)
        return Provisioner(self.settings, runner=self.runner, which=which_factory(present))

    def test_a_plan_is_the_default_and_creates_nothing(self):
        provisioner = self.build(("docker",))
        outcome = provisioner.apply(provisioner.plan(PERSISTENT, "docker"), dry_run=True)
        self.assertFalse(outcome["applied"])
        self.assertEqual(self.runner.commands, [])
        self.assertIn("nothing was charged", outcome["reason"])

    def test_every_target_answers_the_same_two_questions(self):
        provisioner = self.build(("docker", "ssh", "terraform", "aws", "celesto"))
        for target, kind in (("docker", PERSISTENT), ("celesto", PERSISTENT),
                             ("local", DISPOSABLE), ("ssh", PERSISTENT),
                             ("aws", PERSISTENT)):
            spec = {"name": "box", "ssh_host": "anne@10.0.0.4"}
            plan = provisioner.plan(kind, target, spec)
            self.assertEqual((plan.target, plan.kind, plan.name), (target, kind, "box"))
            self.assertTrue(plan.steps, target)
            self.assertTrue(all(s.description for s in plan.steps), target)

    def test_the_cloud_plan_is_terraform_in_your_own_account(self):
        provisioner = self.build(("terraform", "aws"))
        plan = provisioner.plan(PERSISTENT, "aws", {"name": "box", "region": "eu-west-1"})
        write = plan.steps[0]
        self.assertEqual(write.action, "write")
        self.assertIn("aws_instance", write.content)
        self.assertIn('region = "eu-west-1"', write.content)
        self.assertEqual(plan.facts["module"], "aws_eu-west-1_small")
        self.assertEqual([s.command[:2] for s in plan.steps if s.command],
                         [["terraform", "-chdir=aws_eu-west-1_small"],
                          ["terraform", "-chdir=aws_eu-west-1_small"]])

    def test_apply_writes_state_and_runs_the_commands_in_order(self):
        provisioner = self.build(("docker",))
        outcome = provisioner.apply(provisioner.plan(PERSISTENT, "docker", {"name": "box"}),
                                    dry_run=False)
        self.assertTrue(outcome["ok"])
        self.assertEqual(len(self.runner.commands), 1)
        self.assertTrue(self.runner.commands[0][:3] == ["docker", "run", "-d"])
        state_path = os.path.join(provisioner.state_dir, "box", "persistent.json")
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertEqual(state["name"], "box")

    def test_a_failed_step_stops_the_plan_instead_of_pretending(self):
        provisioner = self.build(("terraform", "aws"), fail_on="apply")
        outcome = provisioner.apply(
            provisioner.plan(PERSISTENT, "aws", {"name": "box"}), dry_run=False)
        self.assertFalse(outcome["ok"])
        self.assertEqual(len(self.runner.commands), 2)  # init ran, apply failed

    def test_kinds_are_policed_per_target(self):
        provisioner = self.build(("docker",))
        with self.assertRaises(ValidationError):
            provisioner.plan(DISPOSABLE, "celesto")
        with self.assertRaises(ValidationError):
            provisioner.plan(PERSISTENT, "e2b")
        with self.assertRaises(ValidationError):
            provisioner.plan("permanent", "docker")
        with self.assertRaises(ValidationError):
            provisioner.plan(PERSISTENT, "wat")

    def test_ssh_target_needs_a_host(self):
        provisioner = self.build(("ssh",))
        with self.assertRaises(ValidationError):
            provisioner.plan(PERSISTENT, "ssh")

    def test_ssh_target_runs_on_the_host_you_already_pay_for(self):
        provisioner = self.build(("ssh",))
        plan = provisioner.plan(PERSISTENT, "ssh", {"name": "box", "ssh_host": "anne@10.0.0.4"})
        self.assertEqual(plan.steps[0].command[:4], ["ssh", "-o", "BatchMode=yes", "anne@10.0.0.4"])
        self.assertIn("--restart=always", plan.steps[0].command[-1])

    def test_e2b_embed_says_what_it_needs_out_loud(self):
        provisioner = self.build(("docker",))
        plan = provisioner.plan(DISPOSABLE, "e2b", {"name": "box"})
        self.assertTrue(any("kvm" in note for note in plan.notes))
        self.assertTrue(any("compose" in " ".join(s.command) for s in plan.steps if s.command))

    def test_the_default_target_uses_what_is_installed(self):
        self.assertEqual(self.build(("celesto", "docker")).default_target(PERSISTENT), "celesto")
        self.assertEqual(self.build(("docker",)).default_target(PERSISTENT), "docker")
        self.assertEqual(self.build(()).default_target(PERSISTENT), "local")

    def test_connect_reports_what_is_actually_missing(self):
        outcome = self.build(("terraform",)).connect("aws")
        self.assertFalse(outcome["ready"])
        self.assertTrue(outcome["binaries"]["terraform"])
        self.assertFalse(outcome["binaries"]["aws"])
        self.assertIn("install what is missing", outcome["next"])
        self.assertTrue(os.path.exists(outcome["state_file"]))

    def test_connect_refuses_a_provider_it_cannot_see(self):
        with self.assertRaises(ValidationError):
            self.build(()).connect("cloudflare")


if __name__ == "__main__":
    unittest.main()
