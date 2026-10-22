"""Decrypt MeshCore hashtag-channel group text (payload type 5).

Wire layout of the payload (the last `payload_len` bytes of the raw packet,
whatever the path prefix is): 1 byte channel hash, 2 byte MAC, AES-128-ECB
ciphertext. A hashtag channel's secret is sha256(name)[:16]; its channel hash
is sha256(secret)[0] and the MAC is HMAC-SHA256 (key = secret padded to 32
bytes) over the ciphertext, truncated to 2 bytes. Plaintext is a 4 byte LE
timestamp, 1 flags byte, then "sender: text" NUL padded.
"""
import hashlib
import hmac
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

GROUP_TEXT = 5


@dataclass(frozen=True)
class Channel:
    name: str
    secret: bytes
    hash: int


@dataclass(frozen=True)
class Decoded:
    channel: str
    sender: str
    text: str
    sent_at: int


def channel(name: str) -> Channel:
    secret = hashlib.sha256(name.encode()).digest()[:16]
    return Channel(name, secret, hashlib.sha256(secret).digest()[0])


def parse_channels(spec: str) -> list[Channel]:
    return [channel(n if n.startswith("#") else f"#{n}")
            for n in (s.strip() for s in spec.split(",")) if n]


def decode(packet: dict, channels: list[Channel]) -> Decoded | None:
    """Return the message if `packet` is group text on one of `channels`."""
    try:
        if int(packet["packet_type"]) != GROUP_TEXT:
            return None
        size = int(packet["payload_len"])
        payload = bytes.fromhex(packet["raw"])[-size:] if size > 0 else b""
    except (KeyError, ValueError, TypeError):
        return None
    if len(payload) < 3 + 16:
        return None
    mac, ciphertext = payload[1:3], payload[3:]
    for ch in channels:
        # The channel hash is one byte, so it only narrows the candidates;
        # the MAC is what proves the key.
        if payload[0] != ch.hash:
            continue
        want = hmac.new(ch.secret + b"\0" * 16, ciphertext, hashlib.sha256).digest()[:2]
        if not hmac.compare_digest(want, mac):
            continue
        usable = ciphertext[: len(ciphertext) // 16 * 16]
        plain = Cipher(algorithms.AES(ch.secret), modes.ECB()).decryptor().update(usable)
        sent_at = int.from_bytes(plain[:4], "little")
        body = plain[5:].split(b"\0", 1)[0].decode("utf-8", "replace")
        sender, sep, text = body.partition(": ")
        if not sep:
            sender, text = "", body
        return Decoded(ch.name, sender, text, sent_at)
    return None
