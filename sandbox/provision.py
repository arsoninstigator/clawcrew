"""provisioning, native to crittertown.

`crittertown get persistent` and `crittertown get disposable` should work from a laptop, a
vps, a bare metal box, or a cloud account, and they should never surprise anyone
with a bill. so every target answers the same question the same way:

1. **plan** - a list of steps you can read: files to write, commands to run. this is
   the default. `crittertown get persistent` prints it and stops.
2. **apply** - only with `--apply`, and only through `runner`, so a test can prove
   what would run without running it.

nothing here calls a vendor sdk. byoc means the machine or the account is yours and
the software is open source: persistent and ephemeral runtime are adapted through their own
clis and compose files, and the cloud targets emit terraform you keep in your repo.
when a provider needs something the host cannot give (kvm for firecracker, qemu for
persistent local), the plan says so in `notes` instead of failing three steps later.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from core.config import Settings
from create.plans import Plan, Step, record_plan, run_plan
from core.db import now
from core.errors import ValidationError

PERSISTENT = "persistent"
DISPOSABLE = "disposable"
KINDS = (PERSISTENT, DISPOSABLE)


def _terraform(provider: str, spec: dict) -> tuple[str, str]:
    name = spec["name"]
    region = spec["region"]
    machine = spec.get("machine", "small")
    if provider == "aws":
        return f"aws_{region}_{machine}", f"""# crittertown persistent computer on aws, in your account, in your state file.
provider "aws" {{ region = "{region}" }}

resource "aws_security_group" "crittertown" {{
  name        = "crittertown-{name}"
  description = "crittertown computer, no public ingress except ssh"
  ingress {{ from_port = 22 to_port = 22 protocol = "tcp" cidr_blocks = ["0.0.0.0/0"] }}
  egress  {{ from_port = 0 to_port = 0 protocol = "-1" cidr_blocks = ["0.0.0.0/0"] }}
}}

resource "aws_instance" "computer" {{
  ami                    = var.ami
  instance_type          = var.instance_type
  key_name               = var.key_name
  vpc_security_group_ids = [aws_security_group.crittertown.id]
  user_data              = file("${{path.module}}/cloud-init.yaml")
  tags = {{ Name = "crittertown-{name}" }}
}}

output "host" {{ value = aws_instance.computer.public_ip }}
"""
    if provider == "gcp":
        return f"gcp_{region}_{machine}", f"""# crittertown computer on gcp
provider "google" {{ project = var.project region = "{region}" }}

resource "google_compute_instance" "computer" {{
  name         = "crittertown-{name}"
  machine_type = "{machine}"
  zone         = var.zone
  boot_disk {{ initialize_params {{ image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64" }} }}
  network_interface {{ access_config {{}} }}
  metadata = {{ ssh-keys = var.ssh_keys, user-data = file("${{path.module}}/cloud-init.yaml") }}
}}

output "host" {{ value = google_compute_instance.computer.network_interface[0].access_config[0].nat_ip }}
"""
    if provider == "azure":
        return f"azure_{region}_{machine}", f"""# crittertown computer on azure
provider "azurerm" {{ features {{}} }}

resource "azurerm_resource_group" "crittertown" {{
  name     = "crittertown-{name}"
  location = "{region}"
}}

resource "azurerm_linux_virtual_machine" "computer" {{
  name                = "crittertown-{name}"
  resource_group_name = azurerm_resource_group.crittertown.name
  location            = azurerm_resource_group.crittertown.location
  size                = "{machine}"
  admin_username      = var.admin_username
  network_interface_ids = [azurerm_network_interface.crittertown.id]
  admin_ssh_key {{ username = var.admin_username public_key = var.ssh_public_key }}
  os_disk {{ caching = "ReadWrite" storage_account_type = "Standard_LRS" }}
  source_image_reference {{
    publisher = "Canonical" offer = "ubuntu-24_04-lts" sku = "server" version = "latest"
  }}
  custom_data = base64encode(file("${{path.module}}/cloud-init.yaml"))
}}

output "host" {{ value = azurerm_linux_virtual_machine.computer.public_ip_address }}
"""
    return f"oci_{region}_{machine}", f"""# crittertown computer on oracle cloud (always free tier shapes work here)
provider "oci" {{ region = "{region}" }}

resource "oci_core_instance" "computer" {{
  compartment_id      = var.compartment_id
  availability_domain = var.availability_domain
  shape               = "{machine}"
  source_details {{
    source_type = "image"
    source_id   = var.image_id
  }}
  metadata = {{ ssh_authorized_keys = var.ssh_public_key, user_data = base64encode(file("${{path.module}}/cloud-init.yaml")) }}
}}

output "host" {{ value = oci_core_instance.computer.public_ip }}
"""


CLOUD_INIT = """#cloud-config
package_update: true
packages: [docker.io, curl, git]
runcmd:
  - systemctl enable --now docker
  - mkdir -p /opt/crittertown
  - docker run -d --restart=always --name crittertown-computer -v /opt/crittertown:/work \
      {image} sleep infinity
"""


class Provisioner:
    """the one place that knows how to get a machine on each kind of target."""

    def __init__(
        self,
        settings: Settings,
        runner: Callable[[list[str]], dict] | None = None,
        which: Callable[[str], str | None] | None = None,
    ):
        self.settings = settings
        self._runner = runner or self._subprocess_runner
        self._which = which or shutil.which
        self.state_dir = os.path.join(
            os.path.dirname(os.path.abspath(settings.database_path)), "provisioning")

    # ---- discovery -----------------------------------------------------
    @staticmethod
    def _subprocess_runner(command: list[str]) -> dict:
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=1800)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"command": command, "ok": False, "stdout": "", "stderr": str(exc)}
        return {"command": command, "ok": done.returncode == 0,
                "stdout": done.stdout[-4000:], "stderr": done.stderr[-2000:]}

    def targets(self) -> list[dict]:
        found = {name: bool(self._which(name)) for name in
                 ("terraform", "docker", "ssh", "kubectl", "persistent", "aws", "gcloud",
                  "az", "oci")}
        return [
            {"target": "local", "kinds": [PERSISTENT, DISPOSABLE],
             "needs": ["persistent (local microvm) or docker"],
             "available": found["persistent"] or found["docker"]},
            {"target": "persistent", "kinds": [PERSISTENT],
             "needs": ["persistent cli", "PERSISTENT_API_KEY for cloud"],
             "available": found["persistent"]},
            {"target": "ephemeral", "kinds": [DISPOSABLE],
             "needs": ["docker + kvm for the embed runtime, or EPHEMERAL_API_KEY"],
             "available": found["docker"] or bool(self.settings.ephemeral_api_key)},
            {"target": "docker", "kinds": [PERSISTENT, DISPOSABLE],
             "needs": ["docker"], "available": found["docker"]},
            {"target": "hosted", "kinds": [PERSISTENT, DISPOSABLE],
             "needs": ["hosted sdk", "HOSTED_API_KEY (their cloud, opt in)"],
             "available": bool(self.settings.hosted_api_key)},
            {"target": "ssh", "kinds": [PERSISTENT, DISPOSABLE],
             "needs": ["ssh to your own vps, bare metal or personal server"],
             "available": found["ssh"]},
            {"target": "k8s", "kinds": [PERSISTENT, DISPOSABLE],
             "needs": ["kubectl"], "available": found["kubectl"]},
            {"target": "aws", "kinds": [PERSISTENT], "needs": ["terraform", "aws credentials"],
             "available": found["terraform"] and found["aws"]},
            {"target": "gcp", "kinds": [PERSISTENT], "needs": ["terraform", "gcloud credentials"],
             "available": found["terraform"] and found["gcloud"]},
            {"target": "azure", "kinds": [PERSISTENT], "needs": ["terraform", "az login"],
             "available": found["terraform"] and found["az"]},
            {"target": "oracle", "kinds": [PERSISTENT], "needs": ["terraform", "oci config"],
             "available": found["terraform"] and found["oci"]},
        ]

    # ---- plan ----------------------------------------------------------
    def plan(self, kind: str, target: str = "", spec: dict | None = None) -> Plan:
        if kind not in KINDS:
            raise ValidationError(f"kind must be one of {list(KINDS)}")
        spec = dict(spec or {})
        target = target or spec.get("target") or self.default_target(kind)
        spec.setdefault("name", "crittertown-demo")
        spec.setdefault("image", self.settings.persistent_default_image)
        spec.setdefault("region", self.default_region(target))
        builder = getattr(self, f"_plan_{target}", None)
        if builder is None:
            raise ValidationError(
                f"unknown target '{target}'",
                targets=[t["target"] for t in self.targets()])
        plan = builder(kind, spec)
        plan.state_dir = os.path.join(self.state_dir, plan.name)
        return plan

    def default_target(self, kind: str) -> str:
        if kind == PERSISTENT:
            return "persistent" if self._which("persistent") else (
                "docker" if self._which("docker") else "local")
        return "ephemeral" if (self._which("docker") or self.settings.ephemeral_api_key) else "local"

    @staticmethod
    def default_region(target: str) -> str:
        return {"aws": "us-east-1", "gcp": "us-central1-a", "azure": "eastus",
                "oracle": "us-ashburn-1"}.get(target, "local")

    def _plan_local(self, kind: str, spec: dict) -> Plan:
        name = spec["name"]
        if kind == PERSISTENT:
            steps = [
                Step("exec", "create a persistent computer with the persistent oss runtime",
                     ["persistent", "computer", "create", "--name", name, "--image", spec["image"]]),
                Step("exec", "prove it is reachable",
                     ["persistent", "computer", "exec", name, "--", "uname", "-a"]),
            ]
            notes = ["persistent local needs qemu; without it use --target docker"]
        else:
            steps = [
                Step("exec", "run a disposable container, removed on exit",
                     ["docker", "run", "--rm", "-d", "--name", name, spec["image"],
                      "sleep", "infinity"]),
            ]
            notes = ["disposable means gone after the run; nothing is snapshotted"]
        return Plan("local", kind, name, steps, notes=notes)

    def _plan_persistent(self, kind: str, spec: dict) -> Plan:
        if kind == DISPOSABLE:
            raise ValidationError("persistent is the persistent target; use ephemeral or docker for disposable")
        name = spec["name"]
        cloud = bool(self.settings.persistent_api_key)
        steps = [Step("exec", "create a computer with persistent",
                      ["persistent", "computer", "create", "--name", name,
                       "--provider", "cloud" if cloud else "local"])
                 if cloud else
                 Step("exec", "create a computer with persistent",
                      ["persistent", "computer", "create", "--name", name])]
        steps.append(Step("exec", "check the machine before trusting it",
                          ["persistent", "doctor", "--strict", "--json"]))
        notes = ["cloud is opt in: set PERSISTENT_API_KEY to use it, the local microvm otherwise"]
        return Plan("persistent", kind, name, steps, notes=notes,
                    facts={"provider": "cloud" if cloud else "local"})

    def _plan_docker(self, kind: str, spec: dict) -> Plan:
        name = spec["name"]
        volume = ["-v", f"{name}-work:/work"]
        steps = [Step("exec", "start the computer container",
                      ["docker", "run", "-d", "--name", name, *volume, spec["image"],
                       "sleep", "infinity"])]
        if kind == DISPOSABLE:
            steps.append(Step("exec", "remove the container when the task ends",
                              ["docker", "rm", "-f", name]))
            notes = ["a disposable container is destroyed after the run"]
        else:
            notes = [f"files live in the docker volume {name}-work and survive restarts"]
        return Plan("docker", kind, name, steps, notes=notes)

    def _plan_ephemeral(self, kind: str, spec: dict) -> Plan:
        if kind == PERSISTENT:
            raise ValidationError("ephemeral is the disposable target; use persistent for persistent")
        name = spec["name"]
        if self.settings.ephemeral_api_key:
            steps = [Step("exec", "create a firecracker sandbox through the ephemeral api",
                          ["python", "-m", "ingredients.machines.ephemeral.create",
                           "--template", self.settings.ephemeral_default_template])]
            notes = ["ephemeral api key found, so this uses your account, not your machine"]
        else:
            steps = [
                Step("write", "write the ephemeral embed compose file",
                     path="embed/compose.yaml",
                     content="# ephemeral runtime embed, one machine, kvm required\n"
                             "# curl the two files from ephemeral-dev/runtime main/embed/compose\n"),
                Step("exec", "start the self hosted firecracker runtime",
                     ["docker", "compose", "-f", "embed/compose.yaml", "up", "-d", "--wait"]),
            ]
            notes = ["ephemeral embed needs /dev/kvm on this host; a vps without nested "
                     "virtualisation cannot run it, persistent local with qemu can"]
        return Plan("ephemeral", kind, name, steps, notes=notes)

    def _plan_hosted(self, kind: str, spec: dict) -> Plan:
        """their runtime is their cloud, so the plan installs the sdk and creates."""
        name = spec["name"]
        module = "python"
        steps = [
            Step("exec", "install the hosted sdk (apache-2.0, pip)",
                 [module, "-m", "pip", "install", "--upgrade", "hosted"]),
            Step("exec", f"create the {kind} sandbox through their sdk",
                 [module, "-m", "ingredients.machines.hosted.create",
                  "--name", name] + (["--template", spec["template"]]
                                     if spec.get("template") else [])),
        ]
        notes = [
            "hosted's repo is the sdk, console and docs; the firecracker runtime is "
            "their cloud, so this target is the one that is not byoc",
            "for a self hosted runtime use --target persistent (persistent) or "
            "--target ephemeral (disposable)",
        ]
        if not self.settings.hosted_api_key:
            notes.append("set HOSTED_API_KEY before applying")
        return Plan("hosted", kind, name, steps, notes=notes,
                    facts={"base_url": self.settings.hosted_base_url})

    def _plan_ssh(self, kind: str, spec: dict) -> Plan:
        host = spec.get("ssh_host") or ""
        if not host:
            raise ValidationError("ssh target needs spec.ssh_host, for example user@1.2.3.4")
        name = spec["name"]
        user_host = host if "@" in host else f"root@{host}"
        remote = [
            f"docker rm -f {name} || true",
            f"docker run -d --name {name}"
            + (" --restart=always" if kind == PERSISTENT else "")
            + f" -v /opt/crittertown/{name}:/work {spec['image']} sleep infinity",
        ]
        steps = [Step("exec", f"put a {kind} computer on {user_host}",
                      ["ssh", "-o", "BatchMode=yes", user_host, " && ".join(remote)])]
        notes = [f"this host is yours, so it is byoc with no vendor in the loop: {user_host}"]
        return Plan("ssh", kind, name, steps, notes=notes, facts={"host": user_host})

    def _plan_k8s(self, kind: str, spec: dict) -> Plan:
        name = spec["name"]
        manifest = f"""apiVersion: v1
kind: Pod
metadata:
  name: {name}
  labels: {{ app: crittertown, kind: {kind} }}
spec:
  restartPolicy: {"Always" if kind == PERSISTENT else "Never"}
  containers:
    - name: computer
      image: {spec['image']}
      command: ["sleep", "infinity"]
      volumeMounts: [{{ name: work, mountPath: /work }}]
  volumes:
    - name: work
      {"persistentVolumeClaim:" if kind == PERSISTENT else "emptyDir:"}
        {"claimName: " + name if kind == PERSISTENT else "{}"}
"""
        steps = [Step("write", "write the pod manifest", path=f"k8s/{name}.yaml",
                      content=manifest),
                 Step("exec", "apply it", ["kubectl", "apply", "-f", f"k8s/{name}.yaml"])]
        if kind == DISPOSABLE:
            steps.append(Step("exec", "delete the pod after the run",
                              ["kubectl", "delete", "pod", name, "--ignore-not-found"]))
        return Plan("k8s", kind, name, steps)

    def _plan_cloud(self, provider: str, kind: str, spec: dict) -> Plan:
        if kind == DISPOSABLE:
            raise ValidationError(
                f"{provider} is a persistent target; disposable machines belong on ephemeral or docker")
        module, main_tf = _terraform(provider, spec)
        name = spec["name"]
        steps = [
            Step("write", f"write the terraform for {provider}", path=f"{module}/main.tf",
                 content=main_tf),
            Step("write", "write the cloud-init that installs docker and the agent workdir",
                 path=f"{module}/cloud-init.yaml", content=CLOUD_INIT.format(image=spec["image"])),
            Step("exec", "init the module", ["terraform", "-chdir=" + module, "init", "-input=false"]),
            Step("exec", "apply it in your account",
                 ["terraform", "-chdir=" + module, "apply", "-auto-approve"]),
        ]
        notes = [
            "terraform state stays in your repo, so the machine is yours to destroy",
            f"region default {spec['region']}, override with --region",
        ]
        if not self._which("terraform"):
            notes.append("terraform is not installed here; the plan is still valid to read")
        return Plan(provider, kind, name, steps, notes=notes,
                    facts={"provider": provider, "region": spec["region"], "module": module})

    def _plan_aws(self, kind, spec): return self._plan_cloud("aws", kind, spec)
    def _plan_gcp(self, kind, spec): return self._plan_cloud("gcp", kind, spec)
    def _plan_azure(self, kind, spec): return self._plan_cloud("azure", kind, spec)
    def _plan_oracle(self, kind, spec): return self._plan_cloud("oracle", kind, spec)

    # ---- apply ---------------------------------------------------------
    def apply(self, plan: Plan, dry_run: bool = True) -> dict:
        """run the plan. writes land in the state dir, commands go through runner."""
        outcome = run_plan(plan, self._runner, dry_run=dry_run)
        if not dry_run:
            outcome["created_date"] = now()
            record_plan(outcome, plan.state_dir or self.state_dir, f"{plan.kind}.json")
        return outcome

    def destroy(self, kind: str, name: str, target: str = "") -> dict:
        """undo what apply made, by name, without recreating it."""
        plan = self.plan(kind, target, {"name": name})
        commands = {
            "persistent": [["persistent", "computer", "delete", name, "--json"]],
            "local": [["docker", "rm", "-f", name]],
            "docker": [["docker", "rm", "-f", name], ["docker", "volume", "rm", f"{name}-work"]],
            "ephemeral": [["docker", "compose", "-f", "embed/compose.yaml", "down"]],
            "ssh": [["ssh", "-o", "BatchMode=yes", plan.facts.get("host", name),
                     f"docker rm -f {name}"]] if plan.facts.get("host") else [],
            "k8s": [["kubectl", "delete", "pod", name, "--ignore-not-found"]],
        }.get(plan.target, [])
        results = [self._runner(command) for command in commands]
        return {"target": plan.target, "name": name, "destroyed": all(r.get("ok") for r in results),
                "results": results}

    # ---- one click connect --------------------------------------------
    def connect(self, provider: str) -> dict:
        """byoc in one step: detect what this machine already has, remember it, stop."""
        known = {"aws": ["aws", "terraform"], "gcp": ["gcloud", "terraform"],
                 "azure": ["az", "terraform"], "oracle": ["oci", "terraform"],
                 "local": ["docker"], "ssh": ["ssh"], "k8s": ["kubectl"],
                 "persistent": ["persistent"], "bare-metal": ["ssh"], "vps": ["ssh"]}
        if provider not in known:
            raise ValidationError(f"unknown provider '{provider}'", providers=sorted(known))
        found = {name: bool(self._which(name)) for name in known[provider]}
        env_hints = {
            "aws": ["AWS_ACCESS_KEY_ID", "AWS_PROFILE"],
            "gcp": ["GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG"],
            "azure": ["AZURE_CONFIG_DIR"],
            "oracle": ["OCI_CLI_CONFIG_FILE"],
            "persistent": ["PERSISTENT_API_KEY"],
            "ephemeral": ["EPHEMERAL_API_KEY"],
        }.get(provider, [])
        env_found = {key: bool(os.environ.get(key)) for key in env_hints}
        ready = all(found.values()) and (not env_hints or any(env_found.values()))
        record = {
            "provider": provider, "binaries": found, "env": env_found, "ready": ready,
            "connected_date": now(),
            "next": (f"crittertown get persistent --target {provider}"
                     if ready else
                     f"install what is missing ({', '.join(k for k, v in found.items() if not v)})"
                     f" or set credentials, then run crittertown connect {provider} again"),
        }
        os.makedirs(self.state_dir, exist_ok=True)
        path = os.path.join(self.state_dir, f"connect-{provider}.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record, indent=2))
        record["state_file"] = path
        return record
