"""celesto adapter: persistent computers for an agent.

this runs on celesto's **generated** openapi client, vendored in
`clawcrew/vendor/celesto_cloud_api` (Apache-2.0, source in CelestoAI/celesto,
`openapi/cloud.json`). nothing here re-types a route or a json field by hand: the
paths, request bodies and response models come from that client, so when celesto
changes a field the diff shows up in the vendored package, not in string literals
scattered through this file.

the endpoints it drives:

    POST   /v1/computers                    ComputerCreateRequest -> ComputerResponse
    GET    /v1/computers/{id}               ComputerResponse
    POST   /v1/computers/{id}/exec          ComputerExecRequest -> ComputerExecResponse
    POST   /v1/computers/{id}/stop | /start ComputerResponse
    DELETE /v1/computers/{id}

three details the generated client already encodes and that a hand-rolled client
tends to miss:

* `Idempotency-Key` on create. provisioning an agent's computer twice must not bill
  twice, so the header carries the stable computer key.
* exec returns `exit_code` *and* `timed_out`; a command that hit the wall clock can
  come back with code 0 and truncated output, so both are read.
* auth is `Authorization: Bearer`, same token as the dashboard.

compile a computer from the agent's `runtime` dict: `size_id`, `vcpus`, `ram_mb`,
`disk_size_mb`, `image`, `template_id`, `template_version`, `network_policy`.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..errors import ProviderFailure
from ..vendor.celesto_cloud_api.api.computers import (
    create_computer_v1_computers_post as create_op,
    delete_computer_v1_computers_computer_id_delete as delete_op,
    exec_on_computer_v1_computers_computer_id_exec_post as exec_op,
    get_computer_v1_computers_computer_id_get as get_op,
    list_computers_v1_computers_get as list_op,
    start_computer_v1_computers_computer_id_start_post as start_op,
    stop_computer_v1_computers_computer_id_stop_post as stop_op,
)
from ..vendor.celesto_cloud_api.models.computer_create_request import ComputerCreateRequest
from ..vendor.celesto_cloud_api.models.computer_exec_request import ComputerExecRequest
from ..vendor.celesto_cloud_api.models.computer_exec_response import ComputerExecResponse
from ..vendor.celesto_cloud_api.models.computer_list_response import ComputerListResponse
from ..vendor.celesto_cloud_api.models.computer_response import ComputerResponse
from ..vendor.celesto_cloud_api.models.network_policy import NetworkPolicy
from ..vendor.celesto_cloud_api.models.network_policy_mode import NetworkPolicyMode
from .base import BaseProvider, ExecResult, Handle
from .transport import DEFAULT_CELESTO_BASE, call, celesto_client, field, guard, unwrap

# fields accepted by ComputerCreateRequest, in the order a runtime dict sets them
CREATE_FIELDS = (
    "size_id",
    "vcpus",
    "ram_mb",
    "disk_size_mb",
    "image",
    "template_id",
    "template_version",
    "external_volume_enabled",
)

READY_STATUSES = {"running", "ready", "active", "started"}
DEAD_STATUSES = {"error", "lost", "deleted"}


class CelestoProvider(BaseProvider):
    name = "celesto"
    kind = "persistent"
    isolated = True

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_CELESTO_BASE,
        image: str = "",
        client: str = "clawcrew",
        transport: httpx.BaseTransport | None = None,
    ):
        self.api_key = api_key or ""
        self.base_url = base_url or DEFAULT_CELESTO_BASE
        self.image = image
        self.client_tag = client
        self._transport = transport
        self._api = None

    # ---- client -------------------------------------------------------
    @property
    def api(self):
        if self._api is None:
            self._api = celesto_client(
                self.api_key, self.base_url, client_tag=self.client_tag, transport=self._transport
            )
        return self._api

    def configured(self) -> bool:
        return bool(self.api_key)

    # ---- translation --------------------------------------------------
    def build_spec(self, spec: dict) -> dict:
        """translate a clawcrew runtime dict into a ComputerCreateRequest body.
        a plain string network policy becomes the vendor's object."""
        spec = spec or {}
        body: dict[str, Any] = {}
        for name in CREATE_FIELDS:
            if spec.get(name) is not None:
                body[name] = spec[name]
        if not body.get("image") and self.image:
            body["image"] = self.image
        policy = spec.get("network_policy")
        if isinstance(policy, str):
            mode = policy.strip().lower()
            body["network_policy"] = NetworkPolicy(
                mode=NetworkPolicyMode(mode) if mode in {"open", "off"} else NetworkPolicyMode.OFF
            )
        elif isinstance(policy, dict) and policy.get("mode"):
            body["network_policy"] = NetworkPolicy(mode=NetworkPolicyMode(str(policy["mode"])))
        return body

    def handle_from(self, computer: ComputerResponse, key: str = "") -> Handle:
        connection = field(computer, "connection")
        return Handle(
            provider=self.name,
            kind="persistent",
            ref=str(computer.id),
            meta={
                "key": key,
                "name": field(computer, "name", ""),
                "status": str(field(computer, "status", "unknown")),
                "vcpus": field(computer, "vcpus", 0),
                "ram_mb": field(computer, "ram_mb", 0),
                "disk_size_mb": field(computer, "disk_size_mb", 0),
                "image": field(computer, "image", ""),
                "template_id": field(computer, "template_id", ""),
                "ssh": field(connection, "ssh", "") if connection else "",
                "access_url": field(connection, "access_url", "") if connection else "",
            },
        )

    # ---- lifecycle ----------------------------------------------------
    def ensure(self, key: str, spec: dict) -> Handle:
        self._require()
        body = ComputerCreateRequest(**self.build_spec(spec))
        response = call(
            create_op.sync_detailed,
            client=self.api,
            body=body,
            idempotency_key=key or None,
            what=f"creating computer '{key}'",
        )
        computer = unwrap(response, ComputerResponse, f"creating computer '{key}'")
        handle = self.handle_from(computer, key)
        if not handle.ref:
            raise ProviderFailure("celesto did not return a computer id")
        return handle

    def probe(self, handle: Handle) -> dict:
        self._require()
        response = call(
            get_op.sync_detailed,
            handle.ref,
            client=self.api,
            what=f"reading computer {handle.ref}",
        )
        return unwrap(response, ComputerResponse, f"reading computer {handle.ref}").to_dict()

    def is_ready(self, handle: Handle) -> bool:
        try:
            status = str(self.probe(handle).get("status", "")).lower()
        except ProviderFailure:
            return False
        return status in READY_STATUSES

    def start(self, handle: Handle) -> dict:
        self._require()
        response = call(
            start_op.sync_detailed, handle.ref, client=self.api, what=f"starting {handle.ref}"
        )
        return unwrap(response, ComputerResponse, f"starting {handle.ref}").to_dict()

    def stop(self, handle: Handle) -> dict:
        self._require()
        response = call(
            stop_op.sync_detailed, handle.ref, client=self.api, what=f"stopping {handle.ref}"
        )
        return unwrap(response, ComputerResponse, f"stopping {handle.ref}").to_dict()

    def list_computers(self, status: str | None = None) -> list[dict]:
        self._require()
        response = call(list_op.sync_detailed, client=self.api, what="listing computers")
        page = unwrap(response, ComputerListResponse, "listing computers")
        items = field(page, "computers", []) or []
        rows = [item.to_dict() for item in items]
        if status:
            rows = [row for row in rows if str(row.get("status", "")).lower() == status.lower()]
        return rows

    def destroy(self, handle: Handle) -> None:
        """persistent computers outlive clawcrew rows; callers must opt in."""
        self._require()
        try:
            response = call(
                delete_op.sync_detailed, handle.ref, client=self.api, what=f"deleting {handle.ref}"
            )
            guard(response, f"deleting {handle.ref}")
        except ProviderFailure as exc:
            if exc.details.get("status") != 404:
                raise
        return None

    # ---- work ---------------------------------------------------------
    def exec(self, handle: Handle, command: str, timeout: int = 300) -> ExecResult:
        self._require()
        started = time.time()
        body = ComputerExecRequest(command=command, timeout=max(1, int(timeout)))
        response = call(
            exec_op.sync_detailed,
            handle.ref,
            client=self.api,
            body=body,
            what=f"exec on {handle.ref}",
        )
        result = unwrap(response, ComputerExecResponse, f"exec on {handle.ref}")
        exit_code = int(result.exit_code)
        if bool(field(result, "timed_out", False)) and exit_code == 0:
            exit_code = 124  # keep the shell convention for "ran out of time"
        duration = field(result, "duration_ms", 0) or int((time.time() - started) * 1000)
        return ExecResult(
            exit_code=exit_code,
            stdout=str(result.stdout or ""),
            stderr=str(result.stderr or ""),
            duration_ms=int(duration),
        )


def make(
    api_key: str,
    base_url: str = DEFAULT_CELESTO_BASE,
    image: str = "",
    transport: httpx.BaseTransport | None = None,
) -> CelestoProvider:
    return CelestoProvider(api_key, base_url, image, transport=transport)
