"""provisioning, native to clawcrew.

`clawcrew get persistent` and `clawcrew get disposable` should work from a laptop, a
vps, a bare metal box, or a cloud account, and they should never surprise anyone
with a bill. so every target answers the same question the same way:

1. **plan** - a list of steps you can read: files to write, commands to run. this is
   the default. `clawcrew get persistent` prints it and stops.
2. **apply** - only with `--apply`, and only through `runner`, so a test can prove
   what would run without running it.

nothing here calls a vendor sdk. byoc means the machine or the account is yours and
the software is open source: celesto and e2b runtime are adapted through their own
clis and compose files, and the cloud targets emit terraform you keep in your repo.
when a provider needs something the host cannot give (kvm for firecracker, qemu for
celesto local), the plan says so in `notes` instead of failing three steps later.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

from clawcrew.config import Settings
from clawcrew.create.plans import Plan, Step, record_plan, run_plan
from clawcrew.db import now
from clawcrew.errors import ValidationError

PERSISTENT = "persistent"
DISPOSABLE = "disposable"
KINDS = (PERSISTENT, DISPOSABLE)


def _terraform(provider: str, spec: dict) -> tuple[str, str]:
    name = spec["name"]
    region = spec["region"]
    machine = spec.get("machine", "small")
    if provider == "aws":
        return f"aws_{region}_{machine}", f"""# clawcrew persistent computer on aws, in your account, in your state file.
provider "aws" {{ region = "{region}" }}

resource "aws_security_group" "clawcrew" {{
  name        = "clawcrew-{name}"
  description = "clawcrew computer, no public ingress except ssh"
  ingress {{ from_port = 22 to_port = 22 protocol = "tcp" cidr_blocks = ["0.0.0.0/0"] }}
  egress  {{ from_port = 0 to_port = 0 protocol = "-1" cidr_blocks = ["0.0.0.0/0"] }}
}}

resource "aws_instance" "computer" {{
  ami                    = var.ami
  instance_type          = var.instance_type
  key_name               = var.key_name
  vpc_security_group_ids = [aws_security_group.clawcrew.id]
  user_data              = file("${{path.module}}/cloud-init.yaml")
  tags = {{ Name = "clawcrew-{name}" }}
}}

output "host" {{ value = aws_instance.computer.public_ip }}
"""
    if provider == "gcp":
        return f"gcp_{region}_{machine}", f"""# clawcrew computer on gcp
provider "google" {{ project = var.project region = "{region}" }}

resource "google_compute_instance" "computer" {{
  name         = "clawcrew-{name}"
  machine_type = "{machine}"
  zone         = var.zone
  boot_disk {{ initialize_params {{ image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64" }} }}
  network_interface {{ access_config {{}} }}
  metadata = {{ ssh-keys = var.ssh_keys, user-data = file("${{path.module}}/cloud-init.yaml") }}
}}

output "host" {{ value = google_compute_instance.computer.network_interface[0].access_config[0].nat_ip }}
"""
    if provider == "azure":
        return f"azure_{region}_{machine}", f"""# clawcrew computer on azure
provider "azurerm" {{ features {{}} }}

resource "azurerm_resource_group" "clawcrew" {{
  name     = "clawcrew-{name}"
  location = "{region}"
}}

resource "azurerm_linux_virtual_machine" "computer" {{
  name                = "clawcrew-{name}"
  resource_group_name = azurerm_resource_group.clawcrew.name
  location            = azurerm_resource_group.clawcrew.location
  size                = "{machine}"
  admin_username      = var.admin_username
  network_interface_ids = [azurerm_network_interface.clawcrew.id]
  admin_ssh_key {{ username = var.admin_username public_key = var.ssh_public_key }}
  os_disk {{ caching = "ReadWrite" storage_account_type = "Standard_LRS" }}
  source_image_reference {{
    publisher = "Canonical" offer = "ubuntu-24_04-lts" sku = "server" version = "latest"
  }}
  custom_data = base64encode(file("${{path.module}}/cloud-init.yaml"))
}}

output "host" {{ value = azurerm_linux_virtual_machine.computer.public_ip_address }}
"""
    return f"oci_{region}_{machine}", f"""# clawcrew computer on oracle cloud (always free tier shapes work here)
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
  - mkdir -p /opt/clawcrew
  - docker run -d --restart=always --name clawcrew-computer -v /opt/clawcrew:/work \
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
                 ("terraform", "docker", "ssh", "kubectl", "celesto", "aws", "gcloud",
                  "az", "oci")}
        return [
            {"target": "local", "kinds": [PERSISTENT, DISPOSABLE],
             "needs": ["celesto (local microvm) or docker"],
             "available": found["celesto"] or found["docker"]},
            {"target": "celesto", "kinds": [PERSISTENT],
             "needs": ["celesto cli", "CELESTO_API_KEY for cloud"],
             "available": found["celesto"]},
            {"target": "e2b", "kinds": [DISPOSABLE],
             "needs": ["docker + kvm for the embed runtime, or E2B_API_KEY"],
             "available": found["docker"] or bool(self.settings.e2b_api_key)},
            {"target": "docker", "kinds": [PERSISTENT, DISPOSABLE],
             "needs": ["docker"], "available": found["docker"]},
            {"target": "superserve", "kinds": [PERSISTENT, DISPOSABLE],
             "needs": ["superserve sdk", "SUPERSERVE_API_KEY (their cloud, opt in)"],
             "available": bool(self.settings.superserve_api_key)},
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
        spec.setdefault("name", "clawcrew-demo")
        spec.setdefault("image", self.settings.celesto_default_image)
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
            return "celesto" if self._which("celesto") else (
                "docker" if self._which("docker") else "local")
        return "e2b" if (self._which("docker") or self.settings.e2b_api_key) else "local"

    @staticmethod
    def default_region(target: str) -> str:
        return {"aws": "us-east-1", "gcp": "us-central1-a", "azure": "eastus",
                "oracle": "us-ashburn-1"}.get(target, "local")

    def _plan_local(self, kind: str, spec: dict) -> Plan:
        name = spec["name"]
        if kind == PERSISTENT:
            steps = [
                Step("exec", "create a persistent computer with the celesto oss runtime",
                     ["celesto", "computer", "create", "--name", name, "--image", spec["image"]]),
                Step("exec", "prove it is reachable",
                     ["celesto", "computer", "exec", name, "--", "uname", "-a"]),
            ]
            notes = ["celesto local needs qemu; without it use --target docker"]
        else:
            steps = [
                Step("exec", "run a disposable container, removed on exit",
                     ["docker", "run", "--rm", "-d", "--name", name, spec["image"],
                      "sleep", "infinity"]),
            ]
            notes = ["disposable means gone after the run; nothing is snapshotted"]
        return Plan("local", kind, name, steps, notes=notes)

    def _plan_celesto(self, kind: str, spec: dict) -> Plan:
        if kind == DISPOSABLE:
            raise ValidationError("celesto is the persistent target; use e2b or docker for disposable")
        name = spec["name"]
        cloud = bool(self.settings.celesto_api_key)
        steps = [Step("exec", "create a computer with celesto",
                      ["celesto", "computer", "create", "--name", name,
                       "--provider", "cloud" if cloud else "local"])
                 if cloud else
                 Step("exec", "create a computer with celesto",
                      ["celesto", "computer", "create", "--name", name])]
        steps.append(Step("exec", "check the machine before trusting it",
                          ["celesto", "doctor", "--strict", "--json"]))
        notes = ["cloud is opt in: set CELESTO_API_KEY to use it, the local microvm otherwise"]
        return Plan("celesto", kind, name, steps, notes=notes,
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

    def _plan_e2b(self, kind: str, spec: dict) -> Plan:
        if kind == PERSISTENT:
            raise ValidationError("e2b is the disposable target; use celesto for persistent")
        name = spec["name"]
        if self.settings.e2b_api_key:
            steps = [Step("exec", "create a firecracker sandbox through the e2b api",
                          ["python", "-m", "clawcrew.create.sandbox.e2b_create",
                           "--template", self.settings.e2b_default_template])]
            notes = ["e2b api key found, so this uses your account, not your machine"]
        else:
            steps = [
                Step("write", "write the e2b embed compose file",
                     path="embed/compose.yaml",
                     content="# e2b runtime embed, one machine, kvm required\n"
                             "# curl the two files from e2b-dev/runtime main/embed/compose\n"),
                Step("exec", "start the self hosted firecracker runtime",
                     ["docker", "compose", "-f", "embed/compose.yaml", "up", "-d", "--wait"]),
            ]
            notes = ["e2b embed needs /dev/kvm on this host; a vps without nested "
                     "virtualisation cannot run it, celesto local with qemu can"]
        return Plan("e2b", kind, name, steps, notes=notes)

    def _plan_superserve(self, kind: str, spec: dict) -> Plan:
        """their runtime is their cloud, so the plan installs the sdk and creates."""
        name = spec["name"]
        module = "python"
        steps = [
            Step("exec", "install the superserve sdk (apache-2.0, pip)",
                 [module, "-m", "pip", "install", "--upgrade", "superserve"]),
            Step("exec", f"create the {kind} sandbox through their sdk",
                 [module, "-m", "clawcrew.create.sandbox.superserve_create",
                  "--name", name] + (["--template", spec["template"]]
                                     if spec.get("template") else [])),
        ]
        notes = [
            "superserve's repo is the sdk, console and docs; the firecracker runtime is "
            "their cloud, so this target is the one that is not byoc",
            "for a self hosted runtime use --target celesto (persistent) or "
            "--target e2b (disposable)",
        ]
        if not self.settings.superserve_api_key:
            notes.append("set SUPERSERVE_API_KEY before applying")
        return Plan("superserve", kind, name, steps, notes=notes,
                    facts={"base_url": self.settings.superserve_base_url})

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
            + f" -v /opt/clawcrew/{name}:/work {spec['image']} sleep infinity",
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
  labels: {{ app: clawcrew, kind: {kind} }}
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
                f"{provider} is a persistent target; disposable machines belong on e2b or docker")
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
            "celesto": [["celesto", "computer", "delete", name, "--json"]],
            "local": [["docker", "rm", "-f", name]],
            "docker": [["docker", "rm", "-f", name], ["docker", "volume", "rm", f"{name}-work"]],
            "e2b": [["docker", "compose", "-f", "embed/compose.yaml", "down"]],
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
                 "celesto": ["celesto"], "bare-metal": ["ssh"], "vps": ["ssh"]}
        if provider not in known:
            raise ValidationError(f"unknown provider '{provider}'", providers=sorted(known))
        found = {name: bool(self._which(name)) for name in known[provider]}
        env_hints = {
            "aws": ["AWS_ACCESS_KEY_ID", "AWS_PROFILE"],
            "gcp": ["GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG"],
            "azure": ["AZURE_CONFIG_DIR"],
            "oracle": ["OCI_CLI_CONFIG_FILE"],
            "celesto": ["CELESTO_API_KEY"],
            "e2b": ["E2B_API_KEY"],
        }.get(provider, [])
        env_found = {key: bool(os.environ.get(key)) for key in env_hints}
        ready = all(found.values()) and (not env_hints or any(env_found.values()))
        record = {
            "provider": provider, "binaries": found, "env": env_found, "ready": ready,
            "connected_date": now(),
            "next": (f"clawcrew get persistent --target {provider}"
                     if ready else
                     f"install what is missing ({', '.join(k for k, v in found.items() if not v)})"
                     f" or set credentials, then run clawcrew connect {provider} again"),
        }
        os.makedirs(self.state_dir, exist_ok=True)
        path = os.path.join(self.state_dir, f"connect-{provider}.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record, indent=2))
        record["state_file"] = path
        return record
