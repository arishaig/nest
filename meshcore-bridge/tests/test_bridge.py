import json

from meshcore_bridge import decode
from meshcore_bridge.bridge import Bridge

from .test_decode import REAL, encrypt

PKT_TOPIC = "meshcore/SEA/B8FD/packets"
STATUS_TOPIC = "meshcore/SEA/B8FD/status"


def make(channels="#test", ttl=1800):
    logs, published, now = [], [], [0.0]
    b = Bridge(decode.parse_channels(channels), "meshcore/decoded", ttl,
               logs.append, lambda t, p: published.append((t, json.loads(p))),
               clock=lambda: now[0])
    return b, logs, published, now


def packet(raw_fields, **extra):
    return json.dumps({"hash": "8C28", "origin": "Repeater", "type": "PACKET",
                       "direction": "rx", "len": "37", "SNR": "12.0", "RSSI": "-93",
                       "score": "1000", **raw_fields, **extra}).encode()


def test_every_packet_is_logged_with_numeric_fields():
    b, logs, published, _ = make(channels="#emergency")
    b.handle(PKT_TOPIC, packet(REAL))
    assert len(logs) == 1 and published == []
    rec = logs[0]
    assert rec["kind"] == "packet"
    assert (rec["SNR"], rec["RSSI"], rec["packet_type"], rec["len"]) == (12, -93, 5, 37)
    assert rec["raw"] == REAL["raw"]  # raw stays; only listed channels are decoded


def test_decoded_hit_is_logged_and_published_once_per_hash():
    b, logs, published, now = make()
    for _ in range(3):
        b.handle(PKT_TOPIC, packet(REAL))
    decoded = [r for r in logs if r["kind"] == "decoded"]
    assert [r["duplicate"] for r in decoded] == [False, True, True]
    assert len(published) == 1
    topic, body = published[0]
    assert topic == "meshcore/decoded/test"
    assert (body["sender"], body["text"], body["hash"]) == ("Arishaig", "Test k8s", "8C28")


def test_same_hash_notifies_again_after_ttl():
    b, _, published, now = make(ttl=60)
    b.handle(PKT_TOPIC, packet(REAL))
    now[0] = 61
    b.handle(PKT_TOPIC, packet(REAL))
    assert len(published) == 2


def test_distinct_hashes_each_notify():
    b, _, published, _ = make(channels="#emergency")
    for i in range(2):
        b.handle(PKT_TOPIC, packet(encrypt("#emergency", "A", f"msg {i}"), hash=f"H{i}"))
    assert [p[1]["text"] for p in published] == ["msg 0", "msg 1"]
    assert published[0][0] == "meshcore/decoded/emergency"


def test_status_is_logged_with_stats_intact():
    b, logs, _, _ = make()
    b.handle(STATUS_TOPIC, json.dumps({"status": "online", "stats": {"mqtt_slots_up": 4}}).encode())
    assert logs == [{"kind": "status", "status": "online", "stats": {"mqtt_slots_up": 4}}]


def test_own_republish_is_not_reprocessed():
    b, logs, published, _ = make()
    b.handle("meshcore/decoded/test", b'{"text": "x"}')
    assert logs == [] and published == []


def test_garbage_payload_is_logged_not_raised():
    b, logs, _, _ = make()
    b.handle(PKT_TOPIC, b"\xff not json")
    b.handle(PKT_TOPIC, b"[1, 2]")
    assert [r["kind"] for r in logs] == ["unparsed", "unparsed"]
