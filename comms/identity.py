"""an agent's keypair, and the two names it has.

the point of `identity` is that an agent is not a row with a display name: it
holds a key, the key signs everything it says, and the public half is what other
people and other relays see. minting one is idempotent per agent, so a room can
be joined by an agent that already exists elsewhere.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from core.errors import ValidationError
from comms import bech32
from comms import secp256k1


@dataclass(frozen=True)
class Keypair:
    seckey: str
    pubkey: str

    @property
    def npub(self) -> str:
        return bech32.npub(self.pubkey)

    @property
    def nsec(self) -> str:
        return bech32.nsec(self.seckey)

    def sign(self, message: bytes) -> str:
        return secp256k1.schnorr_sign(message, bytes.fromhex(self.seckey)).hex()

    def public(self) -> dict:
        return {"pubkey": self.pubkey, "npub": self.npub}


def generate() -> Keypair:
    """a fresh key. rejects the negligible invalid scalars rather than looping."""
    while True:
        candidate = secrets.token_bytes(32)
        try:
            pubkey = secp256k1.public_key(candidate)
        except ValueError:
            continue
        return Keypair(seckey=candidate.hex(), pubkey=pubkey.hex())


def from_seckey(seckey_hex: str) -> Keypair:
    try:
        raw = bytes.fromhex((seckey_hex or "").strip())
    except ValueError as exc:
        raise ValidationError("secret key must be hex") from exc
    if len(raw) != 32:
        raise ValidationError("secret key must be 32 bytes")
    try:
        pubkey = secp256k1.public_key(raw)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return Keypair(seckey=raw.hex(), pubkey=pubkey.hex())


def from_nsec(text: str) -> Keypair:
    try:
        hrp, raw = bech32.decode((text or "").strip())
    except ValueError as exc:
        raise ValidationError(f"not an nsec: {exc}") from exc
    if hrp != "nsec" or len(raw) != 32:
        raise ValidationError("not an nsec")
    return from_seckey(raw.hex())


def verify(signature_hex: str, message: bytes, pubkey_hex: str) -> bool:
    try:
        return secp256k1.schnorr_verify(
            bytes.fromhex(signature_hex), message, bytes.fromhex(pubkey_hex)
        )
    except (ValueError, TypeError):
        return False
