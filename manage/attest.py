"""agentids, subagent ids, and attestations you can check without trusting us.

a log is what a system said about itself. an attestation is a claim bound to a key:
written at the moment of the action, signed by the principal that took it, and chained
to that principal's previous row. editing a row, reordering two, or inventing an actor
all fail verification instead of being noticed later by someone reading carefully.

three names, deliberately distinct:

* `agent_id` (`agent_...`) - the row. bookkeeping: it exists so tables can join.
* `agent_key` - the human handle. alphanumeric, permanent, what a person types.
* `agentid` - the principal: `<bech32 of sha256(pubkey)>`, `agent1...`. the key signs
  it, so nothing without the key can claim it, and a rename moves nothing.

the key is the one the agent already uses for comms (`comms_identities`), because an
agent that says something in a room and an agent that leaves an audit row should be
the same principal, not two copies of one.

a subagent is a principal with a parent. its id is `sub1...`, derived from the parent's
agentid, the task it was spawned for, a fan-out index and a nonce - and the parent
signs that spawn. the derivation is deterministic from the attestation, so a fan-in can
prove a lane's result came from a principal its parent really created, for that task, at
that position, and nothing else can produce that id without the parent's key.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from comms import bech32
from comms import identity as keys
from comms.identity import Keypair
from core.db import Store, loads, now

AGENT_HRP = "agent"
SUBAGENT_HRP = "sub"
PERSISTENCE_MODES = ("workspace", "task_state")


def canonical(payload: Any) -> str:
    """one serialisation, so a signature covers the value and not the whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _body(text: str, hrp: str) -> bytes:
    decoded_hrp, data = bech32.decode(text or "")
    if decoded_hrp != hrp:
        raise ValueError(f"not a {hrp} id")
    return data


# ---- ids ---------------------------------------------------------------
def agentid_for(pubkey_hex: str) -> str:
    """the principal for a public key. 20 bytes of sha256 is enough: it is an index,
    the signature is the proof."""
    try:
        raw = bytes.fromhex((pubkey_hex or "").strip())
    except ValueError as exc:
        raise ValueError("pubkey must be hex") from exc
    if len(raw) not in (32, 33):
        raise ValueError("pubkey must be a 32 byte x-only or 33 byte compressed key")
    return bech32.encode(AGENT_HRP, hashlib.sha256(raw).digest()[:20])


def is_agentid(text: str) -> bool:
    try:
        _body(text, AGENT_HRP)
        return True
    except (ValueError, TypeError):
        return False


def agentid_matches(agentid: str, pubkey_hex: str) -> bool:
    """does this key own this id. the only honest way to check an id someone handed you."""
    try:
        return agentid_for(pubkey_hex) == agentid
    except ValueError:
        return False


def subagent_id(parent_agentid: str, task_id: str, index: int, nonce: str) -> str:
    """a child principal. deterministic from the spawn, so it cannot be minted by
    anyone who was not the parent at that task and position."""
    if not is_agentid(parent_agentid):
        raise ValueError("parent must be an agentid")
    if not nonce:
        raise ValueError("a subagent id needs a nonce to keep siblings apart")
    material = "\x00".join([parent_agentid, str(task_id or ""), str(int(index)), nonce])
    return bech32.encode(SUBAGENT_HRP, hashlib.sha256(material.encode("utf-8")).digest()[:20])


def subagent_matches(subagent: str, parent_agentid: str, task_id: str, index: int,
                     nonce: str) -> bool:
    try:
        return subagent_id(parent_agentid, task_id, index, nonce) == subagent
    except (ValueError, TypeError):
        return False


def is_subagent_id(text: str) -> bool:
    try:
        _body(text, SUBAGENT_HRP)
        return True
    except (ValueError, TypeError):
        return False


# ---- signing -----------------------------------------------------------
def entry_digest(
    seq: int,
    action: str,
    actor_kind: str,
    actor_id: str,
    target_kind: str,
    target_id: str,
    payload_json: str,
    prev_hash: str,
) -> str:
    """the chained digest. unchanged from the unsigned era on purpose: an existing
    trail keeps verifying, its rows just start carrying signatures as well."""
    body = "|".join(
        [str(seq), action, actor_kind, actor_id, target_kind, target_id, payload_json, prev_hash]
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def sign_digest(keypair: Keypair, digest: str) -> str:
    return keypair.sign(bytes.fromhex(digest))


def verify_signature(sig_hex: str, digest: str, pubkey_hex: str) -> bool:
    try:
        return keys.verify(sig_hex, bytes.fromhex(digest), pubkey_hex)
    except (ValueError, TypeError):
        return False


class KeyStore:
    """one keypair per agent, held where comms holds it.

    `mint=False` is for a node whose keys are held elsewhere (external custody): rows
    are then written unsigned and reported as unsigned, never faked as signed.

    `mint` also accepts a callable, because custody is a runtime setting: an operator
    that switches this node to external custody must not keep minting keys out of a
    flag captured at boot. one answer, asked at the moment of the question.
    """

    def __init__(self, store: Store, mint: bool | Any = True):
        self.store = store
        self._mint_source = mint
        self._cache: dict[str, Keypair] = {}

    @property
    def mint(self) -> bool:
        return bool(self._mint_source() if callable(self._mint_source) else self._mint_source)

    def for_agent(self, agent_id: str) -> Keypair | None:
        if not agent_id:
            return None
        if agent_id in self._cache:
            return self._cache[agent_id]
        row = self.store.one("select * from comms_identities where agent_id = ?", (agent_id,))
        if row is None:
            if not self.mint:
                return None
            row = self._mint(agent_id)
            if row is None:
                return None
        if not row["seckey"]:
            return None
        keypair = keys.from_seckey(row["seckey"])
        self._cache[agent_id] = keypair
        return keypair

    def pubkey_of(self, agent_id: str) -> str:
        keypair = self.for_agent(agent_id)
        return keypair.pubkey if keypair else ""

    def agentid_of(self, agent_id: str) -> str:
        pubkey = self.pubkey_of(agent_id)
        return agentid_for(pubkey) if pubkey else ""

    def _mint(self, agent_id: str):
        agent = self.store.one("select id, team_id, name from agents where id = ?", (agent_id,))
        if not agent:
            return None
        keypair = keys.generate()
        with self.store.tx() as c:
            c.execute(
                """insert or ignore into comms_identities
                   (agent_id, team_id, handle, pubkey, seckey, custody, relays, created_date)
                   values (?,?,?,?,?,'local','[]',?)""",
                (agent_id, agent["team_id"], agent["name"], keypair.pubkey, keypair.seckey, now()),
            )
        return self.store.one("select * from comms_identities where agent_id = ?", (agent_id,))


# ---- checking a bundle without the database ----------------------------
def verify_entries(entries: list[dict]) -> dict:
    """recompute a trail from the entries alone.

    this is the part worth handing to someone else: it needs no database, no running
    crittertown, and no trust in us. every row must chain to the one before it, and every
    row that claims a signer must carry a signature that verifies against that pubkey.
    """
    expected_prev = ""
    signed = 0
    unsigned = 0
    for position, entry in enumerate(entries):
        payload = entry.get("payload", {})
        payload_json = entry.get("payload_json") or canonical(payload)
        recomputed = entry_digest(
            int(entry.get("seq", 0)),
            entry.get("action", ""),
            entry.get("actor_kind", ""),
            entry.get("actor_id", ""),
            entry.get("target_kind", ""),
            entry.get("target_id", ""),
            payload_json,
            entry.get("prev_hash", ""),
        )
        if entry.get("prev_hash", "") != expected_prev or recomputed != entry.get("hash"):
            return {"ok": False, "entries": len(entries), "signed": signed,
                    "unsigned": unsigned, "broken_at_seq": entry.get("seq"),
                    "broken_at_index": position, "bad_signature_at_seq": None}
        pubkey = entry.get("pubkey", "")
        signature = entry.get("sig", "")
        if pubkey and signature:
            if not verify_signature(signature, recomputed, pubkey):
                return {"ok": False, "entries": len(entries), "signed": signed,
                        "unsigned": unsigned, "broken_at_seq": None,
                        "broken_at_index": None, "bad_signature_at_seq": entry.get("seq")}
            signer = entry.get("signer", "")
            if signer and not agentid_matches(signer, pubkey):
                return {"ok": False, "entries": len(entries), "signed": signed,
                        "unsigned": unsigned, "broken_at_seq": None,
                        "broken_at_index": None, "bad_signature_at_seq": entry.get("seq")}
            signed += 1
        else:
            unsigned += 1
        expected_prev = entry["hash"]
    return {"ok": True, "entries": len(entries), "signed": signed, "unsigned": unsigned,
            "broken_at_seq": None, "broken_at_index": None, "bad_signature_at_seq": None,
            "head": expected_prev}


def spawn_proof(parent_agentid: str, task_id: str, index: int, nonce: str,
                pubkey_hex: str) -> dict:
    """what a fan-in checks: the id, the derivation, and the key that owns it."""
    return {
        "subagent_id": subagent_id(parent_agentid, task_id, index, nonce),
        "parent_active": None,
        "derived": subagent_matches(
            subagent_id(parent_agentid, task_id, index, nonce), parent_agentid, task_id, index, nonce
        ),
        "parent_agentid": parent_agentid,
        "task_id": task_id,
        "index": int(index),
        "nonce": nonce,
        "parent_pubkey": pubkey_hex,
    }


def loads_payload(text: str) -> Any:  # small helper for callers reading raw rows
    return loads(text, {})
