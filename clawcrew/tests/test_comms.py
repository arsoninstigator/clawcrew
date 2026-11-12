"""comms: keys, signatures, rooms where agents are the members, and the bridges.

the shape of the tests follows the claims in the docs, in order: the curve is
the real one, an event id is the nip-01 id, a room will not take a message from
an agent that is not in it, an outside event has to verify before it is stored,
and a bridge is either wired or refuses.
"""

from __future__ import annotations

import hashlib
import json
import unittest

from base import AppCase

from comms import bech32, events as spec, identity, secp256k1
from comms.events import Event
from comms.transports import (
    HttpBridgeTransport,
    NostrTransport,
    TransportError,
    build_transport,
    _websocket_connector,
)
from core.config import Settings
from core.errors import NotAllowed, NotFound, ProviderNotConfigured, ValidationError


class CurveTests(unittest.TestCase):
    def test_known_group_constants(self) -> None:
        # seckey 1 is the generator: its x coordinate is a published constant, so a
        # wrong scalar multiply cannot pass this
        self.assertEqual(
            secp256k1.public_key(bytes.fromhex("00" * 31 + "01")).hex(),
            "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
        )
        # 2G, also published: catches a broken doubling
        self.assertEqual(
            secp256k1.point_mul((secp256k1.GX, secp256k1.GY), 2)[0],
            0xC6047F9441ED7D6D3045406E95C07CD85C778E4B8CEF3CA7ABAC09B95C709EE5,
        )

    def test_signature_round_trip_and_tamper(self) -> None:
        key = hashlib.sha256(b"anne").digest()
        pubkey = secp256k1.public_key(key)
        message = hashlib.sha256(b"a room message").digest()
        signature = secp256k1.schnorr_sign(message, key)
        self.assertTrue(secp256k1.schnorr_verify(signature, message, pubkey))
        self.assertFalse(secp256k1.schnorr_verify(signature, hashlib.sha256(b"other").digest(), pubkey))
        flipped = bytes([signature[0] ^ 1]) + signature[1:]
        self.assertFalse(secp256k1.schnorr_verify(flipped, message, pubkey))
        self.assertFalse(secp256k1.schnorr_verify(signature[:63], message, pubkey))
        self.assertFalse(secp256k1.schnorr_verify(signature, message, b"\x00" * 32))

    def test_every_key_parity_signs(self) -> None:
        # the parity case is where a schnorr implementation quietly breaks, so it is
        # exercised on many keys rather than one
        for index in range(8):
            keypair = identity.from_seckey(hashlib.sha256(f"k{index}".encode()).hexdigest())
            message = hashlib.sha256(f"m{index}".encode()).digest()
            self.assertTrue(
                secp256k1.schnorr_verify(
                    bytes.fromhex(keypair.sign(message)), message, bytes.fromhex(keypair.pubkey)
                )
            )

    def test_bech32_round_trip(self) -> None:
        keypair = identity.from_seckey(hashlib.sha256(b"bech").hexdigest())
        self.assertTrue(keypair.npub.startswith("npub1"))
        self.assertEqual(bech32.pubkey_from_npub(keypair.npub), keypair.pubkey)
        self.assertEqual(identity.from_nsec(keypair.nsec).pubkey, keypair.pubkey)
        with self.assertRaises(ValueError):
            bech32.decode("npub1notvalid")
        with self.assertRaises(ValidationError):
            identity.from_nsec("nsec1broken")


class EventTests(unittest.TestCase):
    def keypair(self):
        return identity.from_seckey(hashlib.sha256(b"event").hexdigest())

    def test_id_is_the_nip01_serialization(self) -> None:
        keypair = self.keypair()
        event = Event.build(keypair.pubkey, spec.ROOM_MESSAGE, "hello", [["h", "room_1"]],
                            created_at=1700000000)
        expected = hashlib.sha256(json.dumps(
            [0, keypair.pubkey, 1700000000, 9, [["h", "room_1"]], "hello"],
            separators=(",", ":"), ensure_ascii=False,
        ).encode()).hexdigest()
        self.assertEqual(event.id, expected)
        self.assertEqual(len(event.id), 64)

    def test_sign_verify_and_tamper(self) -> None:
        keypair = self.keypair()
        event = Event.build(keypair.pubkey, spec.ROOM_MESSAGE, "hello", [["h", "r"]]).sign(keypair)
        self.assertTrue(event.verify())
        event.content = "hello there"
        self.assertFalse(event.verify())

    def test_signing_with_the_wrong_key_is_refused(self) -> None:
        mine = self.keypair()
        theirs = identity.from_seckey(hashlib.sha256(b"other").hexdigest())
        event = Event.build(mine.pubkey, spec.ROOM_MESSAGE, "hi")
        with self.assertRaises(ValidationError):
            event.sign(theirs)

    def test_filters(self) -> None:
        keypair = self.keypair()
        event = Event.build(keypair.pubkey, spec.ROOM_MESSAGE, "hi", [["h", "r1"]],
                            created_at=100)
        self.assertTrue(spec.matches(event, {"kinds": [9], "#h": ["r1"]}))
        self.assertFalse(spec.matches(event, {"kinds": [4110]}))
        self.assertFalse(spec.matches(event, {"authors": ["00" * 32]}))
        self.assertFalse(spec.matches(event, {"since": 200}))
        self.assertFalse(spec.matches(event, {"until": 50}))
        self.assertTrue(spec.matches(event, {"ids": [event.id]}))
        self.assertTrue(spec.matches(event, {}))

    def test_long_messages_are_refused_by_the_codec(self) -> None:
        with self.assertRaises(ValidationError):
            spec.message("x" * (spec.MESSAGE_LIMIT + 1), "r1")


class AvatarCase(AppCase):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.crew("growth")
        self.researcher = self.agent(self.team["id"], "researcher")
        self.writer = self.agent(self.team["id"], "writer", role="writing", skills=["writing"])
        self.room = self.app.channels.create(
            self.team["id"], "research", members=[self.researcher["id"], self.writer["id"]],
            lead=self.researcher["id"],
        )


class IdentityTests(AvatarCase):
    def test_minting_is_idempotent_and_public_only(self) -> None:
        first = self.app.comms.identity(self.researcher["id"])
        second = self.app.comms.identity(self.researcher["id"])
        self.assertEqual(first["pubkey"], second["pubkey"])
        self.assertTrue(first["npub"].startswith("npub1"))
        self.assertNotIn("seckey", first)
        self.assertEqual(self.app.comms.whois(first["pubkey"])["agent_id"], self.researcher["id"])

    def test_agents_get_different_keys(self) -> None:
        a = self.app.comms.identity(self.researcher["id"])
        b = self.app.comms.identity(self.writer["id"])
        self.assertNotEqual(a["pubkey"], b["pubkey"])

    def test_external_custody_refuses_to_mint(self) -> None:
        # a key that already exists stays readable: it is the operator's key, not ours
        # to delete. what an external-custody node must never do is mint a new one.
        # flip it on the settings object the app was built with: custody is read live,
        # so one switch moves comms and the audit trail together.
        self.app.settings.comms_custody = "external"
        exists = self.app.comms.identity(self.writer["id"])
        self.assertTrue(exists["pubkey"])
        fresh = self.crews.add_agent(self.team["id"], "no-key-yet")
        with self.app.store.tx() as c:
            c.execute("delete from comms_identities where agent_id = ?", (fresh["id"],))
        self.assertEqual(self.app.crews.mint_identity(fresh["id"])["agentid"], "")
        with self.assertRaises(ProviderNotConfigured):
            self.app.comms.identity(fresh["id"])

    def test_missing_agent_is_404(self) -> None:
        with self.assertRaises(NotFound):
            self.app.comms.identity("agent_nope")


class RoomTests(AvatarCase):
    def test_a_room_roster_is_agents_with_keys(self) -> None:
        members = self.app.comms.sync_members(self.room["id"])
        self.assertEqual({m["member_id"] for m in members},
                         {self.researcher["id"], self.writer["id"]})
        self.assertTrue(all(m["pubkey"] for m in members))
        roles = {m["member_id"]: m["role"] for m in members}
        self.assertEqual(roles[self.researcher["id"]], "lead")

    def test_humans_are_off_by_default_and_can_be_enabled(self) -> None:
        with self.assertRaises(NotAllowed):
            self.app.comms.add_member(self.room["id"], "someone@example.com", "human")
        self.app.comms.settings = Settings(
            database_path=self.settings.database_path, comms_humans_enabled=True
        )
        member = self.app.comms.add_member(self.room["id"], "someone@example.com", "human")
        self.assertEqual(member["member_kind"], "human")
        self.assertEqual(member["pubkey"], "")

    def test_joining_is_a_signed_event(self) -> None:
        self.app.comms.sync_members(self.room["id"])
        names = [event["name"] for event in self.app.comms.read(self.room["id"])]
        self.assertEqual(names.count("member.added"), 2)
        added = [e for e in self.app.comms.read(self.room["id"]) if e["name"] == "member.added"][0]
        self.assertTrue(self.app.comms.verify(added["id"])["valid"])

    def test_leaving_is_a_signed_event(self) -> None:
        self.app.comms.sync_members(self.room["id"])
        self.app.comms.remove_member(self.room["id"], self.writer["id"], by=self.researcher["id"])
        self.assertEqual([m["member_id"] for m in self.app.comms.members(self.room["id"])],
                         [self.researcher["id"]])
        self.assertIn("member.removed", [e["name"] for e in self.app.comms.read(self.room["id"])])

    def test_a_non_member_cannot_speak(self) -> None:
        outsider = self.agent(self.team["id"], "intern", role="intern", skills=["research"])
        self.app.comms.identity(outsider["id"])
        with self.assertRaises(NotAllowed):
            self.app.comms.say(self.room["id"], "let me in", author_id=outsider["id"])


class LogTests(AvatarCase):
    def setUp(self) -> None:
        super().setUp()
        self.app.comms.sync_members(self.room["id"])

    def test_a_message_is_signed_and_verifiable(self) -> None:
        record = self.app.comms.say(self.room["id"], "the funnel is fine", author_id=self.researcher["id"])
        self.assertEqual(record["kind"], spec.ROOM_MESSAGE)
        self.assertEqual(record["author_kind"], "agent")
        self.assertTrue(self.app.comms.verify(record["id"])["valid"])
        wire = self.app.comms.read_json(self.room["id"])[-1]
        self.assertEqual(wire["sig"], record["sig"])
        self.assertEqual(Event.from_dict(wire).recompute_id(), record["id"])

    def test_the_channel_conversation_is_a_view_of_the_log(self) -> None:
        self.app.comms.say(self.room["id"], "from the log", author_id=self.researcher["id"])
        messages = self.app.channels.messages(self.room["id"])
        self.assertEqual([m["body"] for m in messages], ["from the log"])
        self.assertEqual(messages[0]["via"], "comms")

    def test_a_channel_message_is_signed_without_being_doubled(self) -> None:
        self.app.channels.post(self.room["id"], "from the board", author_id=self.writer["id"])
        messages = self.app.channels.messages(self.room["id"])
        self.assertEqual([m["body"] for m in messages], ["from the board"])
        self.assertEqual(messages[0]["via"], "app")
        bodies = [e["content"] for e in self.app.comms.read(self.room["id"]) if e["kind"] == 9]
        self.assertEqual(bodies, ["from the board"])

    def test_a_task_tag_travels_with_the_message(self) -> None:
        task = self.app.channels.create_task(self.room["id"], "draft the launch note")
        record = self.app.comms.say(self.room["id"], "on it", author_id=self.writer["id"],
                                    task_id=task["id"])
        self.assertIn(["task", task["id"]], record["tags"])

    def test_cursors_replay_from_the_start_or_from_now(self) -> None:
        early = self.app.comms.subscribe("feed-start", self.room["id"], from_start=True)
        self.assertEqual(early["seq"], 0)
        late = self.app.comms.subscribe("feed-now", self.room["id"])
        self.assertGreater(late["seq"], 0)
        self.app.comms.say(self.room["id"], "one", author_id=self.researcher["id"])
        self.assertEqual(self.app.comms.poll("feed-start")["count"], 3)  # 2 joins + the message
        self.assertEqual(self.app.comms.poll("feed-now")["count"], 1)
        self.assertEqual(self.app.comms.poll("feed-now")["count"], 0)

    def test_polling_can_peek_without_advancing(self) -> None:
        self.app.comms.say(self.room["id"], "one", author_id=self.researcher["id"])
        self.app.comms.subscribe("peek", self.room["id"], from_start=True)
        self.app.comms.poll("peek", advance=False)
        self.assertEqual(self.app.comms.poll("peek")["count"], 3)

    def test_stats_add_up(self) -> None:
        self.app.comms.say(self.room["id"], "one", author_id=self.researcher["id"])
        stats = self.app.comms.stats(self.room["id"])
        self.assertEqual(stats["members"], 2)
        self.assertEqual(stats["messages"], 1)
        self.assertFalse(stats["humans_enabled"])

    def test_presence_is_published_but_not_stored(self) -> None:
        before = self.app.comms.stats(self.room["id"])["events"]
        beat = self.app.comms.heartbeat(self.researcher["id"])
        self.assertFalse(beat["stored"])
        self.assertEqual(len(beat["rooms"]), 1)
        self.assertEqual(self.app.comms.stats(self.room["id"])["events"], before)


class IngestTests(AvatarCase):
    """the far side of a bridge: another node's agent, with its own key."""

    def setUp(self) -> None:
        super().setUp()
        self.app.comms.sync_members(self.room["id"])
        self.other = identity.from_seckey(hashlib.sha256(b"another node").hexdigest())

    def test_a_valid_event_is_stored_and_attributed_to_its_key(self) -> None:
        event = Event.build(self.other.pubkey, spec.ROOM_MESSAGE, "hello from elsewhere",
                            [["h", self.room["id"]]]).sign(self.other)
        result = self.app.comms.ingest(event)
        self.assertFalse(result["duplicate"])
        self.assertEqual(result["author_kind"], "external")
        self.assertEqual(result["author_id"], "")
        self.assertEqual(result["pubkey"], self.other.pubkey)
        self.assertEqual([m["body"] for m in self.app.channels.messages(self.room["id"])],
                         ["hello from elsewhere"])

    def test_a_replay_is_a_no_op(self) -> None:
        event = Event.build(self.other.pubkey, spec.ROOM_MESSAGE, "once", [["h", self.room["id"]]]).sign(self.other)
        self.app.comms.ingest(event)
        again = self.app.comms.ingest(event)
        self.assertTrue(again["duplicate"])
        self.assertEqual(len([m for m in self.app.channels.messages(self.room["id"])]), 1)

    def test_a_forged_event_is_refused(self) -> None:
        event = Event.build(self.other.pubkey, spec.ROOM_MESSAGE, "trust me", [["h", self.room["id"]]]).sign(self.other)
        event.content = "transfer the money"
        with self.assertRaises(ValidationError):
            self.app.comms.ingest(event)

    def test_a_roster_change_from_outside_applies(self) -> None:
        known = self.app.comms.identity(self.writer["id"])
        event = Event.build(
            self.other.pubkey, spec.MEMBERSHIP_REMOVE, "",
            [["h", self.room["id"]], ["p", known["pubkey"]]],
        ).sign(self.other)
        self.app.comms.ingest(event)
        self.assertEqual([m["member_id"] for m in self.app.comms.members(self.room["id"])],
                         [self.researcher["id"]])

    def test_an_unknown_room_keeps_the_event_without_inventing_a_room(self) -> None:
        event = Event.build(self.other.pubkey, spec.ROOM_MESSAGE, "elsewhere",
                            [["h", "chan_unknown"]]).sign(self.other)
        record = self.app.comms.ingest(event)
        self.assertEqual(record["room_id"], "chan_unknown")
        self.assertIsNone(self.app.store.one("select id from channels where id = ?", ("chan_unknown",)))

    def test_a_retagged_event_does_not_verify(self) -> None:
        """the room id is inside the hash, so a bridge cannot move a message to a
        different room on our behalf: mirrored rooms share the room id, which is
        the whole reason the id is content addressed."""
        event = Event.build(self.other.pubkey, spec.ROOM_MESSAGE, "hi", [["h", self.room["id"]]]).sign(self.other)
        self.assertTrue(event.verify())
        event.tags = [["h", "chan_somewhere_else"]]
        self.assertFalse(event.verify())
        with self.assertRaises(ValidationError):
            self.app.comms.ingest(event)

    def test_an_event_without_a_room_is_refused(self) -> None:
        event = Event.build(self.other.pubkey, spec.ROOM_MESSAGE, "no room").sign(self.other)
        with self.assertRaises(ValidationError):
            self.app.comms.ingest(event)


class FakeConnection:
    """just enough of a websocket to prove the nostr path builds and reads frames."""

    def __init__(self, replies: list[str]):
        self.replies = replies
        self.sent: list[str] = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def recv(self) -> str:
        return self.replies.pop(0) if self.replies else "[]"

    def close(self) -> None:
        self.closed = True


class TransportTests(unittest.TestCase):
    def keypair(self):
        return identity.from_seckey(hashlib.sha256(b"bridge").hexdigest())

    def test_missing_configuration_is_loud(self) -> None:
        with self.assertRaises(ProviderNotConfigured):
            HttpBridgeTransport({})
        with self.assertRaises(ProviderNotConfigured):
            NostrTransport({})
        with self.assertRaises(ValidationError):
            NostrTransport({"relay": "http://not-a-relay"})
        with self.assertRaises(ValidationError):
            build_transport("carrier-pigeon")

    def test_websocket_client_is_an_optional_extra(self) -> None:
        try:
            import websocket  # noqa: F401
        except ImportError:
            with self.assertRaises(ProviderNotConfigured):
                _websocket_connector("wss://relay.example", 1.0)
        else:
            self.skipTest("websocket-client is installed")

    def test_http_bridge_round_trip(self) -> None:
        calls: list[tuple[str, str, dict | None]] = []
        keypair = self.keypair()
        event = Event.build(keypair.pubkey, spec.ROOM_MESSAGE, "hi", [["h", "r1"]]).sign(keypair)

        def fake_http(method: str, url: str, payload: dict | None):
            calls.append((method, url, payload))
            if method == "POST":
                return 200, json.dumps({"accepted": 1})
            return 200, json.dumps({"events": [event.to_dict()]})

        bridge = HttpBridgeTransport({"url": "https://bridge.example"}, http=fake_http)
        self.assertEqual(bridge.publish([event]), 1)
        pulled = bridge.pull("r1", since=0)
        self.assertEqual([e.id for e in pulled], [event.id])
        self.assertIn("room=r1", calls[1][1])

    def test_http_bridge_reports_a_bad_answer(self) -> None:
        bridge = HttpBridgeTransport({"url": "https://bridge.example"},
                                     http=lambda m, u, p: (503, "down"))
        with self.assertRaises(TransportError):
            bridge.pull("r1")

    def test_nostr_frames_are_nip01(self) -> None:
        keypair = self.keypair()
        event = Event.build(keypair.pubkey, spec.ROOM_MESSAGE, "hi", [["h", "r1"]]).sign(keypair)
        transport = NostrTransport({"relay": "wss://relay.example"})
        self.assertEqual(transport.publish_frame(event)[0], "EVENT")
        self.assertEqual(transport.publish_frame(event)[1]["id"], event.id)
        request = transport.request_frame("sub1", "r1", kinds=[9], since=10, limit=20)
        self.assertEqual(request[0], "REQ")
        self.assertEqual(request[2]["#h"], ["r1"])
        self.assertEqual(request[2]["since"], 10)
        self.assertEqual(transport.close_frame("sub1"), ["CLOSE", "sub1"])

    def test_nostr_parses_relay_messages(self) -> None:
        transport = NostrTransport({"relay": "wss://relay.example"})
        keypair = self.keypair()
        event = Event.build(keypair.pubkey, spec.ROOM_MESSAGE, "hi", [["h", "r1"]]).sign(keypair)
        parsed = transport.parse_frame(json.dumps(["EVENT", "sub1", event.to_dict()]))
        self.assertEqual(parsed["type"], "event")
        self.assertEqual(parsed["event"].id, event.id)
        self.assertEqual(transport.parse_frame('["EOSE","sub1"]')["type"], "eose")
        accepted = transport.parse_frame(json.dumps(["OK", event.id, True, ""]))
        self.assertTrue(accepted["accepted"])
        refused = transport.parse_frame(json.dumps(["OK", event.id, False, "blocked: no"]))
        self.assertFalse(refused["accepted"])
        with self.assertRaises(TransportError):
            transport.parse_frame("not json")

    def test_nostr_publish_reads_the_ok_and_pull_reads_to_eose(self) -> None:
        keypair = self.keypair()
        event = Event.build(keypair.pubkey, spec.ROOM_MESSAGE, "hi", [["h", "r1"]]).sign(keypair)
        connection = FakeConnection([json.dumps(["OK", event.id, True, ""])])
        transport = NostrTransport({"relay": "wss://relay.example"}, connector=lambda url, timeout: connection)
        self.assertEqual(transport.publish([event]), 1)
        self.assertTrue(connection.closed)

        puller = FakeConnection([
            json.dumps(["EVENT", "sub", event.to_dict()]),
            json.dumps(["EOSE", "sub"]),
        ])
        reader = NostrTransport({"relay": "wss://relay.example"}, connector=lambda url, timeout: puller)
        self.assertEqual([e.id for e in reader.pull("r1")], [event.id])
        self.assertEqual(json.loads(puller.sent[-1])[0], "CLOSE")

    def test_relay_is_a_nostr_relay_with_a_label(self) -> None:
        transport = build_transport("relay", {"relay": "wss://relay.example"})
        self.assertEqual(transport.name, "relay")
        self.assertIsInstance(transport, NostrTransport)


class SyncTests(AvatarCase):
    def setUp(self) -> None:
        super().setUp()
        self.app.comms.sync_members(self.room["id"])
        self.published: list[str] = []
        self.inbox: list[Event] = []
        self.broken = False
        self.fake = _FakeTransport(self)

    def register(self):
        # the transport registry is injectable for exactly this: no network in tests
        self.app.comms.transports["http"] = self.fake
        return self.app.comms.register_bridge(self.room["id"], "http",
                                             {"url": "https://bridge.example"})

    def test_sync_pushes_what_the_bridge_has_not_seen(self) -> None:
        self.register()
        self.app.comms.say(self.room["id"], "one", author_id=self.researcher["id"])
        result = self.app.comms.sync(self.room["id"])
        self.assertEqual(result["bridges"][0]["pushed"], 3)  # 2 joins + 1 message
        self.assertEqual(len(self.published), 3)
        self.assertEqual(self.app.comms.bridge(self.room["id"], "http")["cursor_seq"], 3)

    def test_sync_pulls_and_dedupes(self) -> None:
        self.register()
        other = identity.from_seckey(hashlib.sha256(b"remote").hexdigest())
        self.inbox = [
            Event.build(other.pubkey, spec.ROOM_MESSAGE, "remote hello",
                        [["h", self.room["id"]]]).sign(other)
        ]
        result = self.app.comms.sync(self.room["id"])
        self.assertEqual(result["bridges"][0]["pulled"], 1)
        again = self.app.comms.sync(self.room["id"])
        self.assertEqual(again["bridges"][0]["pulled"], 0)

    def test_a_failed_sync_keeps_its_cursor_and_its_error(self) -> None:
        self.register()
        self.broken = True
        result = self.app.comms.sync(self.room["id"])
        self.assertIn("unreachable", result["bridges"][0]["error"])
        bridge = self.app.comms.bridge(self.room["id"], "http")
        self.assertEqual(bridge["cursor_seq"], 0)
        self.assertIn("unreachable", bridge["last_error"])

    def test_a_room_with_no_bridge_says_so(self) -> None:
        with self.assertRaises(NotFound):
            self.app.comms.sync(self.room["id"], "http")

    def test_a_configured_relay_bridges_new_rooms_automatically(self) -> None:
        self.app.comms.settings = Settings(
            database_path=self.settings.database_path, comms_relay="wss://relay.example"
        )
        other_room = self.app.channels.create(self.team["id"], "ops", members=[self.writer["id"]])
        bridge = self.app.comms.bridge(other_room["id"], "nostr")
        self.assertEqual(bridge["config"]["relay"], "wss://relay.example")


class _FakeTransport:
    name = "http"
    readonly = False

    def __init__(self, case: SyncTests):
        self.case = case

    def publish(self, events):
        if self.case.broken:
            raise TransportError("bridge unreachable")
        self.case.published.extend(event.id for event in events)
        return len(events)

    def pull(self, room_id, since=0, limit=50):
        if self.case.broken:
            raise TransportError("bridge unreachable")
        events, self.case.inbox = [e for e in self.case.inbox if e.created_at >= since], []
        return events


class CommsApiTests(AvatarCase):
    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient

        from api.routes import create_api

        self.client = TestClient(create_api(self.app))
        self.app.comms.sync_members(self.room["id"])

    def test_identity_endpoint_never_returns_a_secret(self) -> None:
        body = self.client.get(f"/comms/agents/{self.researcher['id']}/identity").json()
        self.assertEqual(body["agent_id"], self.researcher["id"])
        self.assertNotIn("seckey", body)

    def test_say_and_read_over_http(self) -> None:
        posted = self.client.post(f"/comms/rooms/{self.room['id']}/messages",
                                  json={"body": "over http"}).json()
        self.assertEqual(posted["content"], "over http")
        listed = self.client.get(f"/comms/rooms/{self.room['id']}/events").json()
        self.assertTrue(any(e["content"] == "over http" for e in listed["events"]))
        wire = self.client.get(f"/comms/rooms/{self.room['id']}/events?wire=true").json()
        self.assertIn("sig", wire["events"][0])
        verified = self.client.get(f"/comms/events/{posted['id']}/verify").json()
        self.assertTrue(verified["valid"])

    def test_joining_a_room_over_http(self) -> None:
        outsider = self.agent(self.team["id"], "intern", role="intern", skills=["research"])
        joined = self.client.post(f"/comms/rooms/{self.room['id']}/members",
                                  json={"agent_id": outsider["id"]}).json()
        self.assertEqual(joined["member_id"], outsider["id"])
        left = self.client.delete(f"/comms/rooms/{self.room['id']}/members/{outsider['id']}").json()
        self.assertEqual(left["removed"], outsider["id"])

    def test_a_human_member_is_refused_with_a_message_that_explains_why(self) -> None:
        response = self.client.post(f"/comms/rooms/{self.room['id']}/members",
                                    json={"member_id": "owner@example.com", "kind": "human"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("first class", response.json()["message"])

    def test_ingest_verifies_before_it_stores(self) -> None:
        other = identity.from_seckey(hashlib.sha256(b"api").hexdigest())
        event = Event.build(other.pubkey, spec.ROOM_MESSAGE, "from a relay",
                            [["h", self.room["id"]]]).sign(other)
        response = self.client.post("/comms/ingest", json=event.to_dict())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "from a relay")
        event.content = "tampered"
        self.assertEqual(self.client.post("/comms/ingest", json=event.to_dict()).status_code, 422)

    def test_counts_report_the_log(self) -> None:
        counts = self.client.get("/health").json()["counts"]
        self.assertIn("comms_events", counts)
        self.assertEqual(counts["comms_members"], 2)


if __name__ == "__main__":
    unittest.main()
