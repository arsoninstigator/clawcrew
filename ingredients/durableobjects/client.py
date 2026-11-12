"""terse's durable actors: the remote shape of coordination, and how to run it yourself.

`TerseAI/durable-actors` (mit) is an open source stand in for cloudflare durable objects:
a class with **durable state** and **serialized execution**, streamed to clients over a
websocket. self hosting is a helm chart on gke with postgres and one gcs bucket, and the
controller listens on `:7100` behind your own ingress.

what that maps onto here, precisely:

| crittertown          | actor          | why                                            |
|-------------------|----------------|------------------------------------------------|
| one crew's board  | `CrewBoard`    | serialized claims over cards and shift slots   |
| one agent's inbox | `AgentInbox`   | a stream of messages, readable from a cursor   |
| one channel       | `ChannelRoom`  | broadcast to everyone watching                 |

the protocol both sides implement is `CoordinationBackend` in `service.py`: `claim`,
`release`, `append`, `read`. sqlite is the reference. an actor runtime is a second
backend, not a rewrite, which is why nothing in crittertown imports their sdk yet: the
contract is written down and tested, the transport is a decision you make by running it.

this file does the part that is provable without a cluster: it plans the deploy, and it
publishes the actor contract the deploy has to satisfy.
"""

from __future__ import annotations

from core.config import Settings
from create.plans import Plan, Step, run_plan
from core.errors import ValidationError

CHART = ("https://github.com/TerseAI/durable-actors/releases/download/"
         "v{version}/durable-actors-{version}.tgz")
OCI = "oci://ghcr.io/terseai/charts/durable-actors"
SERVICE = "actors-durable-actors"

REQUIRED = ("public_url", "bucket", "gsa", "zone")


def actor_contract() -> dict:
    """the actors a crew needs, and the state each one has to keep."""
    return {
        "runtime": "terse durable-actors",
        "licence": "mit",
        "actors": {
            "CrewBoard": {
                "key": "crew:{crew_id}",
                "persisted": ["cards", "column_owner", "shift_slot", "handoffs"],
                "interleaved": ["dispatch", "accept_handoff"],
                "serialized": "claim(card) and claim(slot) never overlap for one crew",
                "maps_to": "CoordinationBackend.claim",
            },
            "AgentInbox": {
                "key": "agent:{agent_id}",
                "persisted": ["messages", "cursor"],
                "interleaved": ["send"],
                "serialized": "one writer per agent, readers stream from their cursor",
                "maps_to": "CoordinationBackend.append/read",
            },
            "ChannelRoom": {
                "key": "channel:{channel_id}",
                "persisted": ["transcript", "members"],
                "interleaved": ["post"],
                "serialized": "broadcast to every connected human and agent",
                "maps_to": "CoordinationBackend.append/read",
            },
        },
    }


def _values(spec: dict) -> str:
    return f"""# what the chart needs, from their self-hosting guide.
publicUrl: {spec['public_url']}
placement:
  zone: {spec['zone']}
storage:
  bucket: {spec['bucket']}
serviceAccount:
  googleServiceAccount: {spec['gsa']}
ingress:
  enabled: {"true" if spec.get("ingress", True) else "false"}
  className: {spec.get('ingress_class', 'nginx')}
  tlsSecret: {spec.get('tls_secret', 'actors-tls')}
"""


def plan_runtime(
    settings: Settings,
    public_url: str = "",
    bucket: str = "",
    gsa: str = "",
    zone: str = "",
    namespace: str = "actors",
    version: str = "",
    ingress: bool = True,
) -> Plan:
    """the self host plan: chart, values, rollout. nothing runs until apply."""
    missing = [name for name, value in
               (("public_url", public_url), ("bucket", bucket), ("gsa", gsa), ("zone", zone))
               if not value]
    if missing:
        raise ValidationError(
            "self hosting durable actors needs: " + ", ".join(missing),
            missing=missing,
            hint="their guide calls them publicUrl, storage.bucket, "
                 "serviceAccount.googleServiceAccount and placement.zone")
    version = version or "0.0.0-pin-a-release"
    spec = {"public_url": public_url, "bucket": bucket, "gsa": gsa, "zone": zone,
            "ingress": ingress}
    steps = [
        Step("write", "write the values file the chart expects",
             path=f"{namespace}/actors-values.yaml", content=_values(spec)),
        Step("exec", "install or upgrade the runtime",
             ["helm", "upgrade", "--install", "actors", CHART.format(version=version),
              "--namespace", namespace, "--values",
              f"{namespace}/actors-values.yaml", "--wait", "--timeout", "10m"]),
        Step("exec", "wait for the controller to roll out",
             ["kubectl", "-n", namespace, "rollout", "status", f"deployment/{SERVICE}"]),
    ]
    notes = [
        "their guide targets gke with gvisor sandbox nodes, postgres for the control "
        "plane and one standard gcs bucket for actor state",
        "create the namespace, credentials secret and tls secret first; the chart "
        "expects `actors-credentials` with postgres-url, jwt-signing-key and api-key",
        "a backend that only invokes actors should get the invoke-only key, which the "
        "chart exposes as DURABLE_ACTORS_INVOKE_API_KEY",
        "after it is up, point crittertown at it with DURABLE_ACTORS_URL; the lease and "
        "stream contract in service.py is what a remote backend has to satisfy",
    ]
    plan = Plan("durable-actors", "coordination", "actors", steps, notes=notes,
                facts={"namespace": namespace, "version": version,
                       "public_url": public_url, "endpoint": f"{public_url.rstrip('/')}"})
    plan.state_dir = f"{settings.database_path.rsplit('/', 1)[0]}/coordination"
    return plan


def apply_runtime(
    settings: Settings,
    runner,
    dry_run: bool = True,
    **kwargs,
) -> dict:
    plan = plan_runtime(settings, **kwargs)
    return run_plan(plan, runner, dry_run=dry_run, label="coordination")
