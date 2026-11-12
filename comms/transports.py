"""transports: where a room's events leave the machine, and how they come back.

there are two, and they are different on purpose.

`http` is the generic one. a room publishes `{"events": [...]}` and pulls
`?room=&since=`, and that is the entire contract, so a gateway for any IM product
- octo-channel-gateway, a slack bridge, a queue, an ando-style client - is a
shim the operator writes in their own stack, in their own language, without
touching crittertown. the contract is ours and it is stable, so a bridge cannot be
half-broken by a change here.

`nostr` is the interoperable one. it speaks nip-01 at a relay: EVENT to publish,
REQ to read, and the events are the same signed events this module already
stores. that is what makes a crittertown room readable by `relay`, by any nip-29
client, or by a relay the operator runs themselves. nothing about the agent
identity changes when it crosses that boundary, which is the point: the same
key, the same signature, a different server.

both refuse rather than pretending. a transport with no url, or one that needs a
websocket client that is not installed, raises at construction, not on the first
message.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from core.errors import CritterTownError, ProviderNotConfigured, ValidationError
from comms.events import Event


class TransportError(CritterTownError):
    """the far end answered badly, or not at all."""

    code = "transport_error"
    status = 502


class Transport:
    """the contract a bridge implements. nothing here is asynchronous: a room
    sync is a short conversation with a server that is either up or is not."""

    name = "transport"
    readonly = False

    def __init__(self, config: dict | None = None):
        self.config = dict(config or {})

    def publish(self, events: list[Event]) -> int:  # pragma: no cover - interface
        raise NotImplementedError

    def pull(self, room_id: str, since: int = 0, limit: int = 50) -> list[Event]:  # pragma: no cover
        raise NotImplementedError

    def status(self) -> dict:
        return {"transport": self.name, "readonly": self.readonly}


def _raise_for(status: int, body: str, url: str) -> None:
    raise TransportError(f"{url} returned {status}", body=body[-500:])


class HttpBridgeTransport(Transport):
    """the contract: post events, get events. signing is not the bridge's job, so
    a bridge cannot forge an author."""

    name = "http"

    def __init__(self, config: dict | None = None, http: Callable | None = None):
        super().__init__(config)
        self.url = str(self.config.get("url") or "").rstrip("/")
        if not self.url:
            raise ProviderNotConfigured("http bridge needs a url")
        self.token = str(self.config.get("token") or "")
        self.timeout = float(self.config.get("timeout") or 10)
        self.publish_path = str(self.config.get("publish_path") or "/events")
        self.pull_path = str(self.config.get("pull_path") or "/events")
        self.headers = dict(self.config.get("headers") or {})
        self._http = http or self._urllib

    # -- plumbing ---------------------------------------------------------
    def _urllib(self, method: str, url: str, payload: dict | None) -> tuple[int, str]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"content-type": "application/json", "accept": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        headers.update({str(k): str(v) for k, v in self.headers.items()})
        request = urlrequest.Request(url, data=data, headers=headers, method=method)
        try:
            with urlrequest.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urlerror.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except (urlerror.URLError, OSError) as exc:
            raise TransportError(f"{url} unreachable: {exc}") from exc

    def _call(self, method: str, url: str, payload: dict | None = None) -> dict:
        status, body = self._http(method, url, payload)
        if not 200 <= int(status) < 300:
            _raise_for(int(status), body, url)
        if not body.strip():
            return {}
        try:
            return json.loads(body)
        except ValueError as exc:
            raise TransportError(f"{url} did not return json") from exc

    # -- the contract ------------------------------------------------------
    def publish(self, events: list[Event]) -> int:
        if self.readonly:
            raise ValidationError(f"{self.name} transport is read only")
        if not events:
            return 0
        answer = self._call("POST", self.url + self.publish_path,
                            {"events": [event.to_dict() for event in events]})
        accepted = answer.get("accepted")
        return int(accepted) if isinstance(accepted, int) else len(events)

    def pull(self, room_id: str, since: int = 0, limit: int = 50) -> list[Event]:
        query = urlparse.urlencode({"room": room_id, "since": int(since), "limit": int(limit)})
        answer = self._call("GET", f"{self.url}{self.pull_path}?{query}")
        raw = answer.get("events") or []
        if not isinstance(raw, list):
            raise TransportError("bridge returned a non-list events field")
        return [Event.from_dict(item) for item in raw]


class NostrTransport(Transport):
    """nip-01 over a websocket relay. `relay` is one of these, and so is a relay
    the operator runs on their own machine, and so is anything else that speaks
    the same four messages."""

    name = "nostr"

    def __init__(self, config: dict | None = None, connector: Callable | None = None):
        super().__init__(config)
        self.relay = str(self.config.get("relay") or self.config.get("url") or "").strip()
        if not self.relay:
            raise ProviderNotConfigured("nostr transport needs a relay url")
        if not self.relay.startswith(("ws://", "wss://")):
            raise ValidationError("nostr relay must be a ws:// or wss:// url")
        self.room_tag = str(self.config.get("room_tag") or "h")
        self.timeout = float(self.config.get("timeout") or 10)
        self.auth_event = self.config.get("auth_event") or None
        self._connector = connector or _websocket_connector

    # -- frames ------------------------------------------------------------
    def publish_frame(self, event: Event) -> list:
        return ["EVENT", event.to_dict()]

    def request_frame(self, subscription: str, room_id: str | None, kinds: list[int] | None = None,
                      since: int = 0, limit: int = 50) -> list:
        filters: dict[str, Any] = {"limit": int(limit)}
        if since:
            filters["since"] = int(since)
        if kinds:
            filters["kinds"] = [int(k) for k in kinds]
        if room_id:
            filters[f"#{self.room_tag}"] = [room_id]
        return ["REQ", subscription, filters]

    def close_frame(self, subscription: str) -> list:
        return ["CLOSE", subscription]

    def parse_frame(self, raw: str | bytes) -> dict:
        """relay messages are short arrays: EVENT, OK, EOSE, NOTICE, CLOSED, AUTH."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            frame = json.loads(raw)
        except ValueError as exc:
            raise TransportError("relay sent a non-json frame") from exc
        if not isinstance(frame, list) or not frame:
            raise TransportError("relay frame is not a list")
        kind = str(frame[0]).upper()
        if kind == "EVENT":
            event = Event.from_dict(frame[2] if len(frame) > 2 else frame[1])
            subscription = frame[1] if len(frame) > 2 else ""
            return {"type": "event", "subscription": subscription, "event": event}
        if kind == "OK":
            return {
                "type": "ok",
                "id": frame[1] if len(frame) > 1 else "",
                "accepted": bool(frame[2]) if len(frame) > 2 else False,
                "message": frame[3] if len(frame) > 3 else "",
            }
        if kind == "EOSE":
            return {"type": "eose", "subscription": frame[1] if len(frame) > 1 else ""}
        if kind in {"NOTICE", "CLOSED", "AUTH"}:
            return {"type": kind.lower(), "message": frame[1] if len(frame) > 1 else ""}
        return {"type": "unknown", "frame": frame}

    # -- the contract ------------------------------------------------------
    def _open(self):
        try:
            return self._connector(self.relay, self.timeout)
        except ProviderNotConfigured:
            raise
        except Exception as exc:  # noqa: BLE001 - a socket failure is a transport failure
            raise TransportError(f"cannot reach relay {self.relay}: {exc}") from exc

    def publish(self, events: list[Event]) -> int:
        if not events:
            return 0
        connection = self._open()
        accepted = 0
        try:
            if self.auth_event:
                connection.send(json.dumps(["AUTH", self.auth_event]))
            for event in events:
                connection.send(json.dumps(self.publish_frame(event)))
            deadline = time.time() + self.timeout
            outstanding = {event.id for event in events}
            while outstanding and time.time() < deadline:
                message = self.parse_frame(connection.recv())
                if message["type"] == "ok" and message["id"] in outstanding:
                    outstanding.discard(message["id"])
                    accepted += 1 if message["accepted"] else 0
                    if not message["accepted"]:
                        raise TransportError(
                            f"relay refused {message['id'][:12]}: {message['message']}"
                        )
        finally:
            connection.close()
        return accepted

    def pull(self, room_id: str, since: int = 0, limit: int = 50) -> list[Event]:
        connection = self._open()
        subscription = f"crittertown-{int(time.time())}"
        events: list[Event] = []
        try:
            connection.send(json.dumps(self.request_frame(subscription, room_id, since=since, limit=limit)))
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                message = self.parse_frame(connection.recv())
                if message["type"] == "event":
                    events.append(message["event"])
                elif message["type"] == "eose":
                    break
                elif message["type"] in {"closed", "notice"}:
                    break
            connection.send(json.dumps(self.close_frame(subscription)))
        finally:
            connection.close()
        return events


def _websocket_connector(url: str, timeout: float):
    """the relay speaks websocket, so this needs a websocket client. it is an
    optional extra rather than a hard dependency because a deployment that only
    uses local rooms should not have to install one."""
    try:
        import websocket  # type: ignore
    except ImportError as exc:
        raise ProviderNotConfigured(
            "nostr transport needs a websocket client: pip install websocket-client"
        ) from exc
    return websocket.create_connection(url, timeout=timeout)


TRANSPORTS: dict[str, type[Transport]] = {
    "http": HttpBridgeTransport,
    "nostr": NostrTransport,
    # relay is a nostr relay with an opinionated client; pointing at its relay is
    # the whole integration, so it is the nostr transport with a label
    "relay": NostrTransport,
}


def build_transport(name: str, config: dict | None = None, **kwargs) -> Transport:
    try:
        factory = TRANSPORTS[name]
    except KeyError as exc:
        raise ValidationError(f"unknown transport '{name}', known: {sorted(TRANSPORTS)}") from exc
    transport = factory(config, **kwargs)
    if name == "relay":
        transport.name = "relay"
    return transport
