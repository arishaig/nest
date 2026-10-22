import hashlib
import hmac

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from meshcore_bridge import decode

# A real packet heard by the repeater: group text on #test (2026-10-22).
REAL = {
    "packet_type": "5",
    "payload_len": "35",
    "raw": "1580D98D0309F5D75875B21C4FE793788486BA5CE30792BCC425A6A3FC483C69C4DB93D7F6",
}


def encrypt(name: str, sender: str, text: str, path: bytes = b"") -> dict:
    """Build a group-text packet the way a node would, for channel `name`."""
    ch = decode.channel(name)
    plain = (1234).to_bytes(4, "little") + b"\0" + f"{sender}: {text}".encode()
    plain += b"\0" * (-len(plain) % 16)
    ct = Cipher(algorithms.AES(ch.secret), modes.ECB()).encryptor().update(plain)
    mac = hmac.new(ch.secret + b"\0" * 16, ct, hashlib.sha256).digest()[:2]
    payload = bytes([ch.hash]) + mac + ct
    raw = bytes([0x15, len(path)]) + path + payload
    return {"packet_type": "5", "payload_len": str(len(payload)), "raw": raw.hex()}


def test_real_packet_decodes_on_test_channel():
    hit = decode.decode(REAL, decode.parse_channels("#test"))
    assert hit is not None
    assert (hit.channel, hit.sender, hit.text) == ("#test", "Arishaig", "Test k8s")


def test_real_packet_ignored_when_channel_not_listed():
    assert decode.decode(REAL, decode.parse_channels("#emergency")) is None


def test_channel_names_accept_missing_hash_and_whitespace():
    assert [c.name for c in decode.parse_channels(" emergency , #test ,")] == ["#emergency", "#test"]


def test_emergency_roundtrip_with_path_prefix():
    pkt = encrypt("#emergency", "Someone", "need help at the park", path=b"\x01\x02\x03")
    hit = decode.decode(pkt, decode.parse_channels("#test,#emergency"))
    assert (hit.channel, hit.sender, hit.text) == ("#emergency", "Someone", "need help at the park")


def test_text_without_sender_prefix():
    ch = decode.channel("#emergency")
    plain = (1).to_bytes(4, "little") + b"\0" + b"no sender here"
    plain += b"\0" * (-len(plain) % 16)
    ct = Cipher(algorithms.AES(ch.secret), modes.ECB()).encryptor().update(plain)
    mac = hmac.new(ch.secret + b"\0" * 16, ct, hashlib.sha256).digest()[:2]
    payload = bytes([ch.hash]) + mac + ct
    pkt = {"packet_type": "5", "payload_len": str(len(payload)), "raw": (b"\x15\x00" + payload).hex()}
    hit = decode.decode(pkt, [ch])
    assert (hit.sender, hit.text) == ("", "no sender here")


def test_bad_mac_is_rejected_even_when_channel_hash_matches():
    pkt = encrypt("#emergency", "x", "y")
    raw = bytearray.fromhex(pkt["raw"])
    raw[-17] ^= 0xFF  # flip a ciphertext byte: MAC no longer verifies
    pkt["raw"] = raw.hex()
    assert decode.decode(pkt, decode.parse_channels("#emergency")) is None


def test_non_group_text_and_malformed_packets_are_ignored():
    channels = decode.parse_channels("#test")
    assert decode.decode({**REAL, "packet_type": "4"}, channels) is None
    assert decode.decode({**REAL, "payload_len": "0"}, channels) is None
    assert decode.decode({**REAL, "raw": "zz"}, channels) is None
    assert decode.decode({"packet_type": "5"}, channels) is None
    assert decode.decode({**REAL, "payload_len": "5"}, channels) is None
