"""secp256k1 schnorr (bip340), in pure python.

why in-tree instead of a dependency: an agent's identity in a nostr-shaped log is
a keypair, and the signature has to be the same one any relay, `buzz`, or any
other nip-01 client will accept. the alternative was to invent a private signing
scheme, which would have made "interoperable" a lie. so this is the real curve.

it is not constant time. that is the honest limitation: it runs on the operator's
own machine when minting and signing an agent's events, not on an adversarial
co-tenant, and it is checked against the published group constants in the tests.
"""

from __future__ import annotations

import hashlib

P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
INF = (0, 0)


def _inv(value: int) -> int:
    return pow(value, -1, P)


def _is_on_curve(point: tuple[int, int]) -> bool:
    x, y = point
    return (y * y - x * x * x - 7) % P == 0


def point_add(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int]:
    if a == INF:
        return b
    if b == INF:
        return a
    x1, y1 = a
    x2, y2 = b
    if x1 == x2 and (y1 + y2) % P == 0:
        return INF
    if a == b:
        lam = 3 * x1 * x1 * _inv(2 * y1) % P
    else:
        lam = (y2 - y1) * _inv(x2 - x1) % P
    x3 = (lam * lam - x1 - x2) % P
    return x3, (lam * (x1 - x3) - y1) % P


def point_mul(point: tuple[int, int], scalar: int) -> tuple[int, int]:
    result = INF
    addend = point
    while scalar:
        if scalar & 1:
            result = point_add(result, addend)
        addend = point_add(addend, addend)
        scalar >>= 1
    return result


def lift_x(x: int) -> tuple[int, int] | None:
    """the even-y point for a 32 byte x coordinate, or none if x is not on the curve."""
    if x <= 0 or x >= P:
        return None
    c = (pow(x, 3, P) + 7) % P
    y = pow(c, (P + 1) // 4, P)
    if (y * y) % P != c:
        return None
    return x, y if y % 2 == 0 else P - y


def tagged_hash(tag: str, message: bytes) -> bytes:
    tag_digest = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_digest + tag_digest + message).digest()


def _bip32_bytes(value: int) -> bytes:
    return value.to_bytes(32, "big")


def public_key(seckey: bytes) -> bytes:
    """x-only public key (32 bytes), the shape nostr event ids are built over."""
    if len(seckey) != 32:
        raise ValueError("secret key must be 32 bytes")
    d = int.from_bytes(seckey, "big")
    if not 1 <= d < N:
        raise ValueError("secret key outside the curve order")
    point = point_mul((GX, GY), d)
    if point == INF:
        raise ValueError("secret key produces the point at infinity")
    return _bip32_bytes(point[0])


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def schnorr_sign(message: bytes, seckey: bytes, aux: bytes = b"") -> bytes:
    """bip340. `message` is the 32 byte digest, `aux` may be empty."""
    if len(message) != 32:
        raise ValueError("message must be 32 bytes")
    if len(aux) != 32:
        aux = hashlib.sha256(aux).digest()
    d0 = int.from_bytes(seckey, "big")
    if not 1 <= d0 < N:
        raise ValueError("secret key outside the curve order")
    # the real public point, not its even-y canonical form: the parity of the
    # secret's own point is what decides whether d has to be negated
    point = point_mul((GX, GY), d0)
    if point == INF:
        raise ValueError("secret key produces the point at infinity")
    pubkey = _bip32_bytes(point[0])
    d = d0 if point[1] % 2 == 0 else N - d0
    masked = _xor(_bip32_bytes(d), tagged_hash("BIP0340/aux", aux))
    nonce = int.from_bytes(
        tagged_hash("BIP0340/nonce", masked + pubkey + message), "big"
    ) % N
    if nonce == 0:
        raise ValueError("nonce is zero")
    r_point = point_mul((GX, GY), nonce)
    k = nonce if r_point[1] % 2 == 0 else N - nonce
    rx = _bip32_bytes(r_point[0])
    challenge = int.from_bytes(
        tagged_hash("BIP0340/challenge", rx + pubkey + message), "big"
    ) % N
    signature = rx + _bip32_bytes((k + challenge * d) % N)
    if not schnorr_verify(signature, message, pubkey):
        raise ValueError("produced an invalid signature")
    return signature


def schnorr_verify(signature: bytes, message: bytes, pubkey: bytes) -> bool:
    if len(signature) != 64 or len(message) != 32 or len(pubkey) != 32:
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if r >= P or s >= N:
        return False
    point = lift_x(int.from_bytes(pubkey, "big"))
    if point is None:
        return False
    challenge = int.from_bytes(
        tagged_hash("BIP0340/challenge", signature[:32] + pubkey + message), "big"
    ) % N
    candidate = point_add(point_mul((GX, GY), s), point_mul(point, N - challenge))
    if candidate == INF or candidate[1] % 2 != 0 or candidate[0] != r:
        return False
    return True
