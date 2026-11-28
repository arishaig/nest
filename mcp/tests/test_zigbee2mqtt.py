import contextlib
import json

import pytest

from nest_mcp.tools import zigbee2mqtt as z2m
from helpers import load_tools, patch_http

BASE = "zigbee2mqtt"
INFO = {"version": "2.1.0", "coordinator": {"type": "EZSP", "meta": {"revision": "7.4"}},
        "network": {"channel": 20, "pan_id": 1234},
        "config": {"serial": {"adapter": "ezsp"}, "availability": {"enabled": True}}}
DEVICES = [
    {"ieee_address": "0x00", "type": "Coordinator", "friendly_name": "Coordinator"},
    {"ieee_address": "0xa1", "type": "Router", "friendly_name": "bulb_1", "power_source": "Mains (single phase)",
     "interview_completed": True, "definition": {"vendor": "Third Reality", "model": "3RSL011Z"}},
    {"ieee_address": "0xa2", "type": "Router", "friendly_name": "bulb_2", "power_source": "Mains (single phase)",
     "interview_completed": True, "definition": {"vendor": "Third Reality", "model": "3RSL011Z"}},
    {"ieee_address": "0xb1", "type": "EndDevice", "friendly_name": "door", "power_source": "Battery",
     "interview_completed": True, "definition": {"vendor": "Aqara", "model": "MCCGQ11LM"}},
]
GROUPS = [{"id": 5, "friendly_name": "vanity", "scenes": [{"id": 1}],
           "members": [{"ieee_address": "0xa1", "endpoint": 1}, {"ieee_address": "0xa2", "endpoint": 1}]}]


def ev(topic, payload, retain=False, at=0.0):
    return {"topic": topic, "payload": payload, "retain": retain, "at": at}


class FakeMqtt:
    """Stands in for _Mqtt: ``script`` events are delivered on each collect()."""

    def __init__(self, retained=(), after_publish=()):
        self.events = list(retained)
        self._after = list(after_publish)
        self.subscribed: list[str] = []
        self.published: list[tuple] = []

    async def subscribe(self, topic):
        self.subscribed.append(topic)

    async def publish(self, topic, payload):
        self.published.append((topic, payload))
        self._published = True

    async def collect(self, seconds, until=None):
        if getattr(self, "_published", False):
            self.events += self._after
            self._after = []


def fake_session(monkeypatch, mqtt):
    @contextlib.asynccontextmanager
    async def session():
        yield mqtt
    monkeypatch.setattr(z2m, "_mqtt_session", session)


def fake_registry(monkeypatch, entities):
    @contextlib.asynccontextmanager
    async def session():
        async def call(payload):
            assert payload["type"] == "config/entity_registry/list"
            return entities
        yield call
    monkeypatch.setattr(z2m, "_ws_session", session)


RETAINED = [
    ev(f"{BASE}/bridge/info", INFO, True), ev(f"{BASE}/bridge/devices", DEVICES, True),
    ev(f"{BASE}/bridge/groups", GROUPS, True), ev(f"{BASE}/bridge/state", {"state": "online"}, True),
    ev(f"{BASE}/bulb_1/availability", {"state": "online"}, True),
    ev(f"{BASE}/bulb_2/availability", {"state": "offline"}, True),
]


async def test_bridge_info_summarises_devices_groups_and_ha_entities(monkeypatch):
    fake_session(monkeypatch, FakeMqtt(RETAINED))
    fake_registry(monkeypatch, [
        {"platform": "mqtt", "unique_id": "0xa1_light_zigbee2mqtt", "entity_id": "light.bulb_1"},
        {"platform": "mqtt", "unique_id": "5_light_zigbee2mqtt", "entity_id": "light.vanity"},
        {"platform": "mqtt", "unique_id": "weird-format", "entity_id": "sensor.odd"},
        {"platform": "hue", "unique_id": "0xa2_light_zigbee2mqtt", "entity_id": "light.ignored"},
    ])
    out = await load_tools(z2m)["z2m_bridge_info"]()
    assert out["adapter"] == "ezsp" and out["coordinator"]["type"] == "EZSP"
    assert out["bridge_state"] == "online" and out["availability_enabled"] is True
    assert out["device_count"] == 3  # coordinator excluded
    assert out["devices_not_online"] == ["bulb_2", "door"]  # offline + never-reported
    assert out["devices"][0]["availability"] != "online"  # problems sort first
    grp = out["groups"][0]
    assert grp["members"] == ["bulb_1", "bulb_2"] and grp["member_count"] == 2
    assert grp["ha_entities"] == ["light.vanity"]
    bulb = next(d for d in out["devices"] if d["friendly_name"] == "bulb_1")
    assert bulb["ha_entities"] == ["light.bulb_1"] and bulb["model"] == "3RSL011Z"
    # Non-matching MQTT entities are reported, not silently dropped; other platforms are ignored.
    assert out["ha_mapping"] == {"mqtt_entities": 3, "matched": 2, "unmatched": 1,
                                 "unmatched_examples": [{"entity_id": "sensor.odd", "unique_id": "weird-format"}]}


async def test_bridge_info_survives_registry_failure(monkeypatch):
    fake_session(monkeypatch, FakeMqtt(RETAINED))

    @contextlib.asynccontextmanager
    async def boom():
        raise RuntimeError("no admin")
        yield
    monkeypatch.setattr(z2m, "_ws_session", boom)
    out = await load_tools(z2m)["z2m_bridge_info"]()
    assert out["group_count"] == 1 and out["groups"][0]["ha_entities"] == []
    assert "no admin" in out["ha_mapping"]["error"]  # the failure is visible, not swallowed


async def test_bridge_info_errors_when_z2m_silent(monkeypatch):
    fake_session(monkeypatch, FakeMqtt([]))
    with pytest.raises(ValueError, match="isn't publishing"):
        await load_tools(z2m)["z2m_bridge_info"]()


async def test_probe_get_reports_reply_and_ignores_retained_state(monkeypatch):
    mqtt = FakeMqtt(
        retained=[ev(f"{BASE}/bulb_1", {"state": "ON"}, retain=True)],
        after_publish=[ev(f"{BASE}/bulb_1", {"state": "OFF"}, at=1.5)])
    fake_session(monkeypatch, mqtt)
    out = await load_tools(z2m)["z2m_probe"](target="bulb_1")
    assert mqtt.published == [(f"{BASE}/bulb_1/get", {"state": ""})]
    assert out["verdict"] == "replied" and out["reply"] == {"state": "OFF"}


async def test_probe_no_reply_surfaces_errors_first(monkeypatch):
    mqtt = FakeMqtt(after_publish=[
        ev(f"{BASE}/bridge/logging", {"level": "info", "message": "MQTT publish"}, at=0.1),
        ev(f"{BASE}/bridge/logging",
           {"level": "error", "message": "\x1b[31mNo more available slots MulticastId subscription\x1b[39m"}, at=0.2),
    ])
    fake_session(monkeypatch, mqtt)
    out = await load_tools(z2m)["z2m_probe"](target="vanity")
    assert out["verdict"] == "no_reply_and_errors_logged" and out["reply_seconds"] is None
    assert out["log"][0]["level"] == "error"
    assert out["log"][0]["message"] == "No more available slots MulticastId subscription"  # ANSI stripped


async def test_probe_no_reply_no_errors(monkeypatch):
    fake_session(monkeypatch, FakeMqtt())
    out = await load_tools(z2m)["z2m_probe"](target="bulb_1")
    assert out["verdict"] == "no_reply_no_errors"


async def test_probe_set_needs_payload_and_rejects_wildcards(monkeypatch):
    fake_session(monkeypatch, FakeMqtt())
    probe = load_tools(z2m)["z2m_probe"]
    with pytest.raises(ValueError, match="needs a payload"):
        await probe(target="bulb_1", mode="set")
    with pytest.raises(ValueError, match="invalid Zigbee2MQTT name"):
        await probe(target="#")
    with pytest.raises(ValueError, match="invalid Zigbee2MQTT name"):
        await probe(target="a/+/b")
    with pytest.raises(ValueError, match="mode must be"):
        await probe(target="bulb_1", mode="remove")


async def test_probe_set_publishes_payload(monkeypatch):
    mqtt = FakeMqtt()
    fake_session(monkeypatch, mqtt)
    await load_tools(z2m)["z2m_probe"](target="bulb_1", mode="set", payload={"state": "TOGGLE"})
    assert mqtt.published == [(f"{BASE}/bulb_1/set", {"state": "TOGGLE"})]


async def test_bridge_request_allowlist(monkeypatch):
    fake_session(monkeypatch, FakeMqtt())
    with pytest.raises(ValueError, match="command must be one of"):
        await load_tools(z2m)["z2m_bridge_request"](command="device/remove")


async def test_bridge_request_matches_transaction(monkeypatch):
    mqtt = FakeMqtt()
    fake_session(monkeypatch, mqtt)
    tool = load_tools(z2m)["z2m_bridge_request"]

    orig = mqtt.publish

    async def publish(topic, payload):
        await orig(topic, payload)
        txn = payload["transaction"]
        mqtt._after = [
            ev(f"{BASE}/bridge/response/health_check", {"status": "ok", "data": {"healthy": False}, "transaction": "other"}),
            ev(f"{BASE}/bridge/response/health_check", {"status": "ok", "data": {"healthy": True}, "transaction": txn}),
        ]
    mqtt.publish = publish
    out = await tool(command="health_check")
    assert mqtt.published[0][0] == f"{BASE}/bridge/request/health_check"
    assert out["status"] == "ok" and out["data"] == {"healthy": True}  # not the other client's reply


async def test_bridge_request_timeout(monkeypatch):
    fake_session(monkeypatch, FakeMqtt())
    out = await load_tools(z2m)["z2m_bridge_request"](command="restart")
    assert out["response"] is None and "hung or restarting" in out["note"]


async def test_log_summary_collapses_repeats_and_drops_stack_frames(monkeypatch):
    err = "[2026-11-28 00:00:38] \x1b[31merror\x1b[39m: \tzh:ezsp:cast: No more available slots MulticastId subscription (Error: No available"
    err2 = err.replace("00:00:38", "00:00:43")
    patch_http(monkeypatch, z2m, {"/loki/api/v1/query_range": {"data": {"result": [{"stream": {}, "values": [
        ["1795000000000000000", err],
        ["1795000001000000000", "    at Multicast.subscribe (/app/multicast.ts:56:23)"],
        ["1795000005000000000", err2],
        ["1795000009000000000", "[2026-11-28 00:01:00] \x1b[32minfo\x1b[39m: \tz2m: Started"],
    ]}]}}})
    out = await load_tools(z2m)["z2m_log_summary"]()
    assert out["lines_scanned"] == 4 and out["distinct_messages"] == 2  # stack frame dropped, repeats merged
    assert out["levels_seen"] == {"error": 2, "info": 1}
    top = out["messages"][0]
    assert top["count"] == 2 and "MulticastId" in top["example"] and "\x1b" not in top["example"]
    assert top["first"] < top["last"]


class FakeWs:
    """Plays back HA frames: each send() queues the scripted reply frames."""

    def __init__(self, replies_for):
        self.sent: list[dict] = []
        self._queue: list[str] = []
        self._replies_for = replies_for

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        self._queue += [json.dumps(f) for f in self._replies_for(msg)]

    async def recv(self):
        if not self._queue:
            import asyncio
            await asyncio.sleep(10)  # wait_for turns this into a timeout
        return self._queue.pop(0)


def _ha_frames(msg):
    ok = {"id": msg["id"], "type": "result", "success": True, "result": None}
    if msg["type"] == "mqtt/subscribe":
        # An event frame can arrive before the result frame, and carries the subscription's id.
        return [{"id": msg["id"], "type": "event",
                 "event": {"topic": "zigbee2mqtt/bulb_1", "payload": '{"state":"ON"}', "qos": 0, "retain": True}}, ok]
    return [ok]


async def test_mqtt_client_subscribes_collects_and_publishes():
    ws = FakeWs(_ha_frames)
    client = z2m._Mqtt(ws)
    await client.subscribe("zigbee2mqtt/#")
    await client.publish("zigbee2mqtt/bulb_1/get", {"state": ""})
    await client.collect(0.05)  # nothing more scripted: returns at the deadline, no error
    assert client.events[0]["topic"] == "zigbee2mqtt/bulb_1"
    assert client.events[0]["payload"] == {"state": "ON"} and client.events[0]["retain"] is True
    sub, pub = ws.sent
    assert sub["type"] == "mqtt/subscribe" and sub["topic"] == "zigbee2mqtt/#"
    assert pub["type"] == "call_service" and pub["domain"] == "mqtt" and pub["service"] == "publish"
    assert json.loads(pub["service_data"]["payload"]) == {"state": ""}


async def test_mqtt_client_surfaces_ha_errors():
    def frames(msg):
        return [{"id": msg["id"], "type": "result", "success": False, "error": {"message": "Unauthorized"}}]
    with pytest.raises(ValueError, match="Unauthorized"):
        await z2m._Mqtt(FakeWs(frames)).subscribe("zigbee2mqtt/#")


async def test_mqtt_collect_stops_early_when_condition_met():
    client = z2m._Mqtt(FakeWs(lambda m: []))
    client.events.append(ev("t", 1))
    await client.collect(30, until=lambda evs: len(evs) >= 1)  # returns at once, not after 30s
