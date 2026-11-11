"""bech32 (bip173) so an agent's key has the name people and clients use.

`npub1...` is what goes in a profile line, a channel membership list or a bug
report; the hex form is what goes on the wire. both are the same 32 bytes.
"""

from __future__ import annotations

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
GENERATOR = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]


def _polymod(values: list[int]) -> int:
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= GENERATOR[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _encode_bytes(data: bytes, five_bit: bool = True) -> list[int]:
    """8 -> 5 bit regroups, padded, which is what npub/nsec use."""
    if not five_bit:
        return list(data)
    acc = 0
    bits = 0
    out: list[int] = []
    for byte in data:
        acc = (acc << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append((acc >> bits) & 31)
    if bits:
        out.append((acc << (5 - bits)) & 31)
    return out


def _decode_bytes(values: list[int]) -> bytes:
    acc = 0
    bits = 0
    out = bytearray()
    for value in values:
        acc = (acc << 5) | value
        bits += 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return bytes(out)


def encode(hrp: str, data: bytes) -> str:
    values = _encode_bytes(data)
    payload = values + [0, 0, 0, 0, 0, 0]
    polymod = _polymod(_hrp_expand(hrp) + payload) ^ 1
    checksum = [(polymod >> (5 * (5 - i))) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[v] for v in values + checksum)


def decode(text: str) -> tuple[str, bytes]:
    lowered = text.lower()
    if lowered != text and text.upper() != text:
        raise ValueError("bech32 string mixes case")
    position = lowered.rfind("1")
    if position < 1 or position + 7 > len(lowered):
        raise ValueError("not a bech32 string")
    hrp = lowered[:position]
    try:
        values = [CHARSET.index(c) for c in lowered[position + 1:]]
    except ValueError as exc:
        raise ValueError("invalid bech32 character") from exc
    if _polymod(_hrp_expand(hrp) + values) != 1:
        raise ValueError("bad bech32 checksum")
    return hrp, _decode_bytes(values[:-6])


def npub(pubkey_hex: str) -> str:
    return encode("npub", bytes.fromhex(pubkey_hex))


def nsec(seckey_hex: str) -> str:
    return encode("nsec", bytes.fromhex(seckey_hex))


def pubkey_from_npub(text: str) -> str:
    hrp, data = decode(text)
    if hrp != "npub" or len(data) != 32:
        raise ValueError("not an npub")
    return data.hex()
