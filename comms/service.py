"""rooms where the members are agents, and the log is signed.

the constraint given for this build is explicit: agents are the only first class
citizens of the communication. so a member is an agent with a keypair, a channel
is a room, and a message is an event that agent signed. people are modelled, and
can be enabled, but nothing in the product depends on a human being in the room,
which is the difference between "agents can post here" and "this is a place agents
work".

what that buys, concretely:

- identity is not a display name. every agent has its own key, so a message can be
  attributed, verified, and replayed somewhere else without trusting this process.
- the log is the record. events are content addressed and idempotent, so a room
  can be synced from a relay twice and still have one copy of each message.
- membership is an event too. adding an agent to a room is a signed put-user, so
  a second relay that sees it ends up with the same roster.

the sqlite rows are the local materialisation of that log. they are not the
authority on their own: `ingest` verifies before it stores.
"""

from __future__ import annotations

from typing import Any, Iterable

from comms.channels import ChannelService
from core.db import Store, dumps, loads, now
from core.errors import NotAllowed, NotFound, ProviderNotConfigured, ValidationError
from comms import events as spec
from comms import identity as keys
from comms.events import Event
from comms.transports import Transport, TransportError, build_transport

MEMBER_KINDS = {"agent", "human", "workflow"}
AUTHOR_KINDS = {"agent", "human", "workflow", "external", "system"}


class CommsService:
    def __init__(self, store: Store, settings=None, channels: ChannelService | None = None,
                 transports: dict[str, Transport] | None = None):
        self.store = store
        self.settings = settings
        self.channels = channels
        self.transports = transports or {}
        self.events_hook = None

    # ---- settings shims --------------------------------------------------
    def _cfg(self, name: str, default):
        return getattr(self.settings, name, default) if self.settings is not None else default

    @property
    def custody(self) -> str:
        return str(self._cfg("comms_custody", "local") or "local")

    @property
    def humans_enabled(self) -> bool:
        return bool(self._cfg("comms_humans_enabled", False))

    # ---- identity --------------------------------------------------------
    def identity(self, agent_id: str, handle: str = "") -> dict:
        """the agent's keypair, minted once. this is the moment an agent stops
        being a row and becomes a participant."""
        row = self.store.one("select * from comms_identities where agent_id = ?", (agent_id,))
        if row:
            return self._identity(row)
        agent = self.store.one("select id, team_id, name from agents where id = ?", (agent_id,))
        if not agent:
            raise NotFound(f"agent {agent_id} not found")
        if self.custody != "local":
            raise ProviderNotConfigured(
                "comms custody is " + self.custody + ": keys are held externally and clawcrew "
                "only stores public halves, so it cannot mint one"
            )
        keypair = keys.generate()
        with self.store.tx() as c:
            c.execute(
                """insert into comms_identities
                   (agent_id, team_id, handle, pubkey, seckey, custody, relays, created_date)
                   values (?,?,?,?,?,?,?,?)""",
                (agent_id, agent["team_id"], handle or agent["name"], keypair.pubkey,
                 keypair.seckey, "local", "[]", now()),
            )
        return self.identity(agent_id, handle)

    def identity_of(self, agent_id: str) -> dict | None:
        row = self.store.one("select * from comms_identities where agent_id = ?", (agent_id,))
        return self._identity(row) if row else None

    def whois(self, pubkey: str) -> dict | None:
        row = self.store.one("select * from comms_identities where pubkey = ?", (pubkey,))
        return self._identity(row) if row else None

    def _identity(self, row) -> dict:
        # the agentid is derived from this pubkey rather than stored beside it: the same
        # key signs room events and audit rows, so there is one principal with two proofs
        # and no second identity to keep in step.
        from manage import attest

        return {
            "agent_id": row["agent_id"],
            "team_id": row["team_id"],
            "handle": row["handle"],
            "agentid": attest.agentid_for(row["pubkey"]),
            "pubkey": row["pubkey"],
            "npub": keys.bech32.npub(row["pubkey"]),
            "custody": row["custody"],
            "relays": loads(row["relays"], []),
            "created_date": row["created_date"],
        }

    def keypair(self, agent_id: str) -> keys.Keypair:
        row = self.store.one("select * from comms_identities where agent_id = ?", (agent_id,))
        if not row:
            self.identity(agent_id)
            row = self.store.one("select * from comms_identities where agent_id = ?", (agent_id,))
        if not row or not row["seckey"]:
            raise ProviderNotConfigured(f"agent {agent_id} has no signing key on this node")
        return keys.from_seckey(row["seckey"])

    # ---- membership ------------------------------------------------------
    def add_member(self, channel_id: str, member_id: str, kind: str = "agent",
                   role: str = "member") -> dict:
        if kind not in MEMBER_KINDS:
            raise ValidationError(f"member kind must be one of {sorted(MEMBER_KINDS)}")
        if kind == "human" and not self.humans_enabled:
            raise NotAllowed(
                "humans are not enabled in this build: agents are the only first class "
                "members of a room (set comms_humans_enabled to change that)"
            )
        self._room(channel_id)
        if kind == "agent":
            if not self.store.one("select id from agents where id = ?", (member_id,)):
                raise NotFound(f"agent {member_id} not found")
            pubkey = self.identity(member_id)["pubkey"]
            keypair = self.keypair(member_id)
        else:
            pubkey, keypair = "", None
        existing = self.store.one(
            "select * from comms_members where room_id = ? and member_id = ?",
            (channel_id, member_id),
        )
        if existing and existing["pubkey"] == pubkey and existing["role"] == role:
            return self.member(channel_id, member_id)
        ts = now()
        with self.store.tx() as c:
            c.execute(
                """insert into comms_members (room_id, member_id, member_kind, pubkey, role, joined_date)
                   values (?,?,?,?,?,?)
                   on conflict(room_id, member_id) do update set
                     member_kind = excluded.member_kind, pubkey = excluded.pubkey,
                     role = excluded.role""",
                (channel_id, member_id, kind, pubkey, role, ts),
            )
        self.auto_bridge(channel_id)
        if keypair:
            self.publish(
                channel_id,
                "",
                kind=spec.MEMBERSHIP_ADD,
                author_id=member_id,
                tags=[["p", pubkey], ["role", role], ["kind", kind]],
            )
        return self.member(channel_id, member_id)

    def remove_member(self, channel_id: str, member_id: str, by: str | None = None) -> dict:
        member = self.member(channel_id, member_id)
        actor = by or member_id
        # the removal is signed while the author is still a member: an event that
        # removes its own author cannot be published after the fact
        if self.store.one("select agent_id from comms_identities where agent_id = ?", (actor,)):
            self.publish(
                channel_id,
                "",
                kind=spec.MEMBERSHIP_REMOVE,
                author_id=actor,
                tags=[["p", member["pubkey"]], ["kind", member["member_kind"]]],
            )
        with self.store.tx() as c:
            c.execute(
                "delete from comms_members where room_id = ? and member_id = ?",
                (channel_id, member_id),
            )
        return {"room_id": channel_id, "removed": member_id}

    def member(self, channel_id: str, member_id: str) -> dict:
        row = self.store.one(
            "select * from comms_members where room_id = ? and member_id = ?",
            (channel_id, member_id),
        )
        if not row:
            raise NotFound(f"{member_id} is not a member of {channel_id}")
        return self._member(row)

    def _member(self, row) -> dict:
        return {
            "room_id": row["room_id"],
            "member_id": row["member_id"],
            "member_kind": row["member_kind"],
            "pubkey": row["pubkey"],
            "npub": keys.bech32.npub(row["pubkey"]) if row["pubkey"] else "",
            "role": row["role"],
            "joined_date": row["joined_date"],
        }

    def members(self, channel_id: str) -> list[dict]:
        rows = self.store.query(
            "select * from comms_members where room_id = ? order by joined_date", (channel_id,)
        )
        return [self._member(row) for row in rows]

    def rooms_for(self, member_id: str) -> list[str]:
        rows = self.store.query(
            "select room_id from comms_members where member_id = ? order by joined_date",
            (member_id,),
        )
        return [row["room_id"] for row in rows]

    def auto_bridge(self, channel_id: str) -> dict | None:
        """a deployment with a relay configured wants every room on it; that is
        an intent, not something to retype per room."""
        relay = str(self._cfg("comms_relay", "") or "")
        if not relay:
            return None
        name = str(self._cfg("comms_relay_transport", "nostr") or "nostr")
        existing = self.store.one(
            "select transport from comms_bridges where room_id = ? and transport = ?",
            (channel_id, name),
        )
        if existing:
            return self.bridge(channel_id, name)
        return self.register_bridge(channel_id, name, {"relay": relay, "url": relay})

    def sync_members(self, channel_id: str) -> list[dict]:
        """mirror the channel's agent roster into the room's comms roster. channels
        predate keys, so this is how a room that already has members gets them
        their identities."""
        if not self.channels:
            raise ProviderNotConfigured("channels service not wired")
        for agent in self.channels.members(channel_id):
            self.add_member(channel_id, agent["agent_id"], "agent", agent["role"])
        return self.members(channel_id)

    def _room(self, channel_id: str) -> dict:
        if not self.channels:
            row = self.store.one("select * from channels where id = ?", (channel_id,))
            if not row:
                raise NotFound(f"room {channel_id} not found")
            return dict(row)
        return self.channels.get(channel_id)

    # ---- publishing ------------------------------------------------------
    def publish(self, channel_id: str, content: str, kind: int = spec.ROOM_MESSAGE,
                author_id: str | None = None, tags: Iterable[Iterable[str]] = (),
                created_at: int | None = None, mirror: bool = True) -> dict:
        """sign and append. membership is the gate: a room is not a message board
        anyone can shout at."""
        room = self._room(channel_id)
        author = author_id or next(
            (m["member_id"] for m in self.members(channel_id) if m["member_kind"] == "agent"), ""
        )
        if not author:
            raise ValidationError("a room message needs an author agent")
        member = self.store.one(
            "select * from comms_members where room_id = ? and member_id = ?",
            (channel_id, author),
        )
        if not member:
            raise NotAllowed(f"agent {author} is not a member of room {room['id']}")
        keypair = self.keypair(author)
        event_tags: list[list[str]] = [[str(p) for p in tag] for tag in tags]
        if kind == spec.ROOM_MESSAGE:
            event_tags = spec.message(content, room["id"], event_tags)
        elif not any(tag[0] == "h" for tag in event_tags):
            event_tags = spec.room_tags(room["id"]) + event_tags
        event = Event.build(keypair.pubkey, kind, content or "", event_tags, created_at).sign(keypair)
        record = self._store_event(event, author_id=author, author_kind="agent")
        if self.events_hook:
            self.events_hook(
                "comms.event",
                {"room_id": channel_id, "event": record, "mirror": mirror},
            )
        return record

    def say(self, channel_id: str, body: str, author_id: str | None = None,
            task_id: str | None = None) -> dict:
        """a room message, mirrored into the channel conversation so the board and
        the log are the same conversation seen from two sides."""
        extra = [["task", task_id]] if task_id else []
        return self.publish(channel_id, body, kind=spec.ROOM_MESSAGE, author_id=author_id,
                            tags=extra)

    def _store_event(self, event: Event, author_id: str = "", author_kind: str = "external") -> dict:
        ts = now()
        with self.store.tx() as c:
            c.execute(
                """insert or ignore into comms_events
                   (id, room_id, kind, pubkey, author_id, author_kind, content, tags,
                    created_at, sig, received_at)
                   values (?,?,?,?,?,?,?,?,?,?,?)""",
                (event.id, event.room, event.kind, event.pubkey, author_id, author_kind,
                 event.content, dumps(event.tags), event.created_at, event.sig, ts),
            )
        return self.event(event.id)

    # ---- reading ---------------------------------------------------------
    def event(self, event_id: str) -> dict:
        row = self.store.one("select * from comms_events where id = ?", (event_id,))
        if not row:
            raise NotFound(f"event {event_id} not found")
        return self._event(row)

    def _event(self, row) -> dict:
        return {
            "seq": row["seq"],
            "id": row["id"],
            "room_id": row["room_id"],
            "kind": row["kind"],
            "name": spec.NAMES.get(row["kind"], f"kind.{row['kind']}"),
            "pubkey": row["pubkey"],
            "npub": keys.bech32.npub(row["pubkey"]),
            "author_id": row["author_id"],
            "author_kind": row["author_kind"],
            "content": row["content"],
            "tags": loads(row["tags"], []),
            "created_at": row["created_at"],
            "sig": row["sig"],
            "received_at": row["received_at"],
        }

    def read(self, channel_id: str, since_seq: int = 0, limit: int = 50,
             kinds: list[int] | None = None) -> list[dict]:
        sql = "select * from comms_events where room_id = ? and seq > ?"
        params: list[Any] = [channel_id, int(since_seq)]
        if kinds:
            sql += " and kind in (" + ",".join("?" * len(kinds)) + ")"
            params.extend(int(k) for k in kinds)
        sql += " order by seq asc limit ?"
        params.append(int(limit))
        return [self._event(row) for row in self.store.query(sql, tuple(params))]

    def read_json(self, channel_id: str, since_seq: int = 0, limit: int = 50) -> list[dict]:
        """the wire shape, for a client that wants to verify rather than trust."""
        return [
            Event.from_dict(
                {
                    "id": item["id"], "pubkey": item["pubkey"], "created_at": item["created_at"],
                    "kind": item["kind"], "tags": item["tags"], "content": item["content"],
                    "sig": item["sig"],
                }
            ).to_dict()
            for item in self.read(channel_id, since_seq=since_seq, limit=limit)
        ]

    def verify(self, event_id: str) -> dict:
        item = self.event(event_id)
        event = Event.from_dict(
            {k: item[k] for k in ("id", "pubkey", "created_at", "kind", "tags", "content", "sig")}
        )
        ok = event.verify()
        return {"id": event_id, "valid": ok, "author": item["author_id"] or item["npub"],
                "kind": item["kind"], "name": item["name"]}

    # ---- cursors ---------------------------------------------------------
    def subscribe(self, name: str, channel_id: str, from_start: bool = False) -> dict:
        existing = self.store.one("select * from comms_cursors where name = ?", (name,))
        if existing:
            return self._cursor(existing)
        start = 0 if from_start else int(
            (self.store.one(
                "select coalesce(max(seq), 0) as m from comms_events where room_id = ?",
                (channel_id,),
            ) or {"m": 0})["m"]
        )
        with self.store.tx() as c:
            c.execute(
                "insert into comms_cursors (name, room_id, seq, updated_date) values (?,?,?,?)",
                (name, channel_id, start, now()),
            )
        return self._cursor(self.store.one("select * from comms_cursors where name = ?", (name,)))

    def _cursor(self, row) -> dict:
        return {"name": row["name"], "room_id": row["room_id"], "seq": row["seq"],
                "updated_date": row["updated_date"]}

    def poll(self, name: str, limit: int = 50, advance: bool = True) -> dict:
        cursor = self.store.one("select * from comms_cursors where name = ?", (name,))
        if not cursor:
            raise NotFound(f"subscription {name} not found")
        batch = self.read(cursor["room_id"], since_seq=cursor["seq"], limit=limit)
        if batch and advance:
            with self.store.tx() as c:
                c.execute(
                    "update comms_cursors set seq = ?, updated_date = ? where name = ?",
                    (batch[-1]["seq"], now(), name),
                )
        return {"subscription": name, "room_id": cursor["room_id"], "events": batch,
                "next_seq": batch[-1]["seq"] if batch else cursor["seq"], "count": len(batch)}

    # ---- ingest ----------------------------------------------------------
    def ingest(self, event: Event, verify: bool = True) -> dict:
        """take an event from outside: verify the signature, then keep it. a
        replayed event is a no-op because the id is a hash of its content."""
        if verify and not event.verify():
            raise ValidationError(f"event {event.id[:12]} does not verify against {event.pubkey[:12]}")
        existing = self.store.one("select * from comms_events where id = ?", (event.id,))
        if existing:
            return {**self._event(existing), "duplicate": True}
        room_id = event.room
        if not room_id:
            raise ValidationError("event has no room tag")
        known = self.store.one("select * from comms_identities where pubkey = ?", (event.pubkey,))
        author_id = known["agent_id"] if known else ""
        author_kind = "agent" if known else "external"
        record = self._store_event(event, author_id=author_id, author_kind=author_kind)
        self._apply_membership(event, room_id)
        if self.events_hook:
            self.events_hook(
                "comms.event",
                {"room_id": room_id, "event": record, "mirror": True},
            )
        return {**record, "duplicate": False}

    def _apply_membership(self, event: Event, room_id: str) -> None:
        """a roster change is an event, so a second relay that sees it ends up with
        the same roster. only applied for rooms this node knows about: an event
        for an unknown room stays in the log without inventing a room."""
        if event.kind not in {spec.MEMBERSHIP_ADD, spec.MEMBERSHIP_REMOVE}:
            return
        if not self.store.one("select id from channels where id = ?", (room_id,)):
            return
        pubkeys = [tag[1] for tag in event.tags_of("p") if len(tag) > 1]
        for pubkey in pubkeys:
            known = self.store.one("select * from comms_identities where pubkey = ?", (pubkey,))
            if event.kind == spec.MEMBERSHIP_ADD and known:
                with self.store.tx() as c:
                    c.execute(
                        """insert into comms_members (room_id, member_id, member_kind, pubkey, role, joined_date)
                           values (?,?,?,?,?,?)
                           on conflict(room_id, member_id) do update set role = excluded.role""",
                        (room_id, known["agent_id"], "agent", pubkey,
                         event.tag("role") or "member", now()),
                    )
            elif event.kind == spec.MEMBERSHIP_REMOVE and known:
                with self.store.tx() as c:
                    c.execute(
                        "delete from comms_members where room_id = ? and member_id = ?",
                        (room_id, known["agent_id"]),
                    )

    # ---- bridges ---------------------------------------------------------
    def register_bridge(self, channel_id: str, transport: str, config: dict | None = None) -> dict:
        self._room(channel_id)
        instance = build_transport(transport, config or {})
        with self.store.tx() as c:
            c.execute(
                """insert into comms_bridges (room_id, transport, config, cursor_seq, last_sync, last_error)
                   values (?,?,?,?,?,?)
                   on conflict(room_id, transport) do update set config = excluded.config""",
                (channel_id, instance.name, dumps(config or {}), 0, None, ""),
            )
        return self.bridge(channel_id, instance.name)

    def bridge(self, channel_id: str, transport: str) -> dict:
        row = self.store.one(
            "select * from comms_bridges where room_id = ? and transport = ?",
            (channel_id, transport),
        )
        if not row:
            raise NotFound(f"no {transport} bridge on room {channel_id}")
        return {
            "room_id": row["room_id"], "transport": row["transport"],
            "config": loads(row["config"], {}), "cursor_seq": row["cursor_seq"],
            "last_sync": row["last_sync"], "last_error": row["last_error"],
        }

    def bridges(self, channel_id: str | None = None) -> list[dict]:
        if channel_id:
            return [self.bridge(channel_id, row["transport"]) for row in self.store.query(
                "select transport from comms_bridges where room_id = ?", (channel_id,))]
        return [self.bridge(row["room_id"], row["transport"]) for row in self.store.query(
            "select room_id, transport from comms_bridges order by room_id")]

    def sync(self, channel_id: str, transport: str | None = None) -> dict:
        """two directions, one call: push what the bridge has not seen, then pull
        what we have not seen. the cursor only advances on success, so a failed
        sync retries the same window instead of losing it."""
        rows = self.store.query(
            "select * from comms_bridges where room_id = ?" + (" and transport = ?" if transport else ""),
            (channel_id, transport) if transport else (channel_id,),
        )
        if not rows:
            raise NotFound(f"no bridge on room {channel_id}")
        results = []
        for row in rows:
            instance = self.transports.get(row["transport"]) or build_transport(
                row["transport"], loads(row["config"], {})
            )
            pushed = 0
            pulled = 0
            try:
                outbound = self.read(channel_id, since_seq=row["cursor_seq"], limit=200)
                if outbound and not instance.readonly:
                    pushed = instance.publish(
                        [Event.from_dict({
                            "id": item["id"], "pubkey": item["pubkey"],
                            "created_at": item["created_at"], "kind": item["kind"],
                            "tags": item["tags"], "content": item["content"], "sig": item["sig"],
                        }) for item in outbound]
                    )
                since = outbound[-1]["created_at"] if outbound else int(row["last_sync"] or 0)
                for event in instance.pull(channel_id, since=int(since)):
                    if event.room != channel_id:
                        continue
                    result = self.ingest(event)
                    if not result["duplicate"]:
                        pulled += 1
                with self.store.tx() as c:
                    c.execute(
                        """update comms_bridges set cursor_seq = ?, last_sync = ?, last_error = ''
                           where room_id = ? and transport = ?""",
                        (outbound[-1]["seq"] if outbound else row["cursor_seq"], now(),
                         channel_id, row["transport"]),
                    )
                results.append({"transport": row["transport"], "pushed": pushed,
                                "pulled": pulled, "error": ""})
            except TransportError as exc:
                with self.store.tx() as c:
                    c.execute(
                        "update comms_bridges set last_error = ? where room_id = ? and transport = ?",
                        (exc.message, channel_id, row["transport"]),
                    )
                results.append({"transport": row["transport"], "pushed": 0, "pulled": 0,
                                "error": exc.message})
        return {"room_id": channel_id, "bridges": results}

    # ---- presence --------------------------------------------------------
    def heartbeat(self, agent_id: str) -> dict:
        """ephemeral, on purpose: presence is not history, so it is published but
        never stored. what is stored is what a room would want to read twice."""
        rooms = self.rooms_for(agent_id)
        keypair = self.keypair(agent_id)
        published = []
        for room_id in rooms:
            event = Event.build(
                keypair.pubkey, spec.PRESENCE, "", spec.room_tags(room_id)
            ).sign(keypair)
            published.append({"room_id": room_id, "id": event.id,
                              "npub": keys.bech32.npub(keypair.pubkey)})
        return {"agent_id": agent_id, "rooms": published, "stored": False}

    # ---- overview --------------------------------------------------------
    def stats(self, channel_id: str | None = None) -> dict:
        def count(sql: str, params: tuple = ()) -> int:
            row = self.store.one(sql, params)
            return int(row["c"]) if row else 0

        where = " where room_id = ?" if channel_id else ""
        params = (channel_id,) if channel_id else ()
        return {
            "room_id": channel_id or "",
            "members": count("select count(*) as c from comms_members" + where, params),
            "events": count("select count(*) as c from comms_events" + where, params),
            "messages": count(
                "select count(*) as c from comms_events" + (where + " and " if where else " where ")
                + "kind = ?", params + (spec.ROOM_MESSAGE,)),
            "identities": count("select count(*) as c from comms_identities"),
            "bridges": count("select count(*) as c from comms_bridges" + where, params),
            "custody": self.custody,
            "humans_enabled": self.humans_enabled,
        }
