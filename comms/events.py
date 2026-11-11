"""the event: one signed line, the same shape whether a person or a process wrote it.

this is nip-01 to the byte: `id` is the sha256 of the serialized
`[0, pubkey, created_at, kind, tags, content]`, `created_at` is unix seconds, and
`sig` is a bip340 signature over the id. anything that can read a nostr relay can
read this without a clawcrew decoder, which is the whole reason for not inventing
a private envelope.

the room primitive is nip-29: a group is a room, kind 9 is a message with an `h`
tag naming the room, 9000/9001 are put-user and remove-user, 39000 is room
metadata. clawcrew adds a few kinds of its own in an unregistered range for the
things a workspace does that plain chat does not have (a card moving, a shift
rolling, a gate waiting) and keeps them in tags, so a client that does not know
them still renders the `content`.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from core.errors import ValidationError

# ---- kinds -------------------------------------------------------------
# nip-29 group chat
ROOM_MESSAGE = 9
MEMBERSHIP_ADD = 9000
MEMBERSHIP_REMOVE = 9001
ROOM_META = 39000
# clawcrew's own, unregistered on purpose: a client that does not know the number
# ignores it, and nothing in nip-29 collides with it
CARD = 4110
HANDOFF = 4111
SHIFT = 4112
GATE = 4113
ACTION = 4114
# ephemeral (nip-01 range): presence heartbeats are not stored history
PRESENCE = 24114

NAMES = {
    ROOM_MESSAGE: "message",
    MEMBERSHIP_ADD: "member.added",
    MEMBERSHIP_REMOVE: "member.removed",
    ROOM_META: "room.meta",
    CARD: "card",
    HANDOFF: "handoff",
    SHIFT: "shift",
    GATE: "gate",
    ACTION: "action",
    PRESENCE: "presence",
}
KINDS_BY_NAME = {name: kind for kind, name in NAMES.items()}
EPHEMERAL = {PRESENCE}

MESSAGE_LIMIT = 8000


def canonical(pubkey: str, created_at: int, kind: int, tags: list[list[str]], content: str) -> bytes:
    """the exact bytes an id is hashed from. no whitespace, no ascii escaping."""
    return json.dumps(
        [0, pubkey, created_at, kind, tags, content],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def event_id(pubkey: str, created_at: int, kind: int, tags: list[list[str]], content: str) -> str:
    return hashlib.sha256(canonical(pubkey, created_at, kind, tags, content)).hexdigest()


@dataclass
class Event:
    pubkey: str
    kind: int
    content: str = ""
    tags: list[list[str]] = field(default_factory=list)
    created_at: int = field(default_factory=lambda: int(time.time()))
    id: str = ""
    sig: str = ""

    # ---- construction ----------------------------------------------------
    @classmethod
    def build(cls, pubkey: str, kind: int, content: str = "",
              tags: Iterable[Iterable[str]] = (), created_at: int | None = None) -> "Event":
        clean = [[str(part) for part in tag] for tag in tags]
        stamp = int(created_at if created_at is not None else time.time())
        return cls(
            pubkey=pubkey,
            kind=int(kind),
            content=content or "",
            tags=clean,
            created_at=stamp,
            id=event_id(pubkey, stamp, int(kind), clean, content or ""),
        )

    def sign(self, keypair) -> "Event":
        if keypair.pubkey != self.pubkey:
            raise ValidationError("event pubkey does not match the signing key")
        self.sig = keypair.sign(bytes.fromhex(self.id))
        return self

    # ---- reading ---------------------------------------------------------
    def tag(self, name: str) -> str:
        for tag in self.tags:
            if tag and tag[0] == name:
                return tag[1] if len(tag) > 1 else ""
        return ""

    def tags_of(self, name: str) -> list[list[str]]:
        return [tag for tag in self.tags if tag and tag[0] == name]

    @property
    def room(self) -> str:
        return self.tag("h")

    @property
    def name(self) -> str:
        return NAMES.get(self.kind, f"kind.{self.kind}")

    def recompute_id(self) -> str:
        return event_id(self.pubkey, self.created_at, self.kind, self.tags, self.content)

    def verify(self) -> bool:
        if self.id != self.recompute_id():
            return False
        if len(self.sig) != 128:
            return False
        from comms import identity

        return identity.verify(self.sig, bytes.fromhex(self.id), self.pubkey)

    # ---- wire ------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pubkey": self.pubkey,
            "created_at": self.created_at,
            "kind": self.kind,
            "tags": self.tags,
            "content": self.content,
            "sig": self.sig,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_dict(cls, payload: dict) -> "Event":
        if not isinstance(payload, dict):
            raise ValidationError("event must be a json object")
        try:
            event = cls(
                pubkey=str(payload["pubkey"]),
                kind=int(payload["kind"]),
                content=str(payload.get("content", "")),
                tags=[[str(p) for p in tag] for tag in payload.get("tags", [])],
                created_at=int(payload["created_at"]),
                id=str(payload.get("id", "")),
                sig=str(payload.get("sig", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"malformed event: {exc}") from exc
        return event


def matches(event: Event, filters: dict[str, Any]) -> bool:
    """nip-01 filter matching, the subset that a relay actually applies.

    `ids`, `authors`, `kinds`, `since`, `until` and `#<tag>` are honoured; `limit`
    belongs to the caller because it is about the result set, not one event.
    """
    def any_of(key: str, values: Iterable[str]) -> bool:
        wanted = {str(v) for v in values}
        return not wanted or str(event.to_dict().get(key, "")) in wanted

    if not any_of("id", filters.get("ids") or []):
        return False
    if not any_of("pubkey", filters.get("authors") or []):
        return False
    kinds = {int(k) for k in (filters.get("kinds") or [])}
    if kinds and event.kind not in kinds:
        return False
    if filters.get("since") is not None and event.created_at < int(filters["since"]):
        return False
    if filters.get("until") is not None and event.created_at > int(filters["until"]):
        return False
    for key, wanted in filters.items():
        if not key.startswith("#"):
            continue
        values = {str(v) for v in wanted}
        if values and not (set(tag[1] for tag in event.tags_of(key[1:]) if len(tag) > 1) & values):
            return False
    return True


def room_tags(room_id: str) -> list[list[str]]:
    return [["h", room_id]]


def message(content: str, room_id: str, extra: Iterable[Iterable[str]] = ()) -> list[list[str]]:
    if len(content or "") > MESSAGE_LIMIT:
        raise ValidationError(f"message longer than {MESSAGE_LIMIT} characters")
    return room_tags(room_id) + [[str(p) for p in tag] for tag in extra]
