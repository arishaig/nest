import contextlib
import json

import pytest

from nest_mcp.tools import homeassistant
from helpers import load_tools, patch_http


async def test_list_entities_filters_by_domain_and_sorts(monkeypatch):
    patch_http(monkeypatch, homeassistant, {
        "/api/states": [
            {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen"}},
            {"entity_id": "sensor.temp", "state": "21", "attributes": {}},
            {"entity_id": "light.bed", "state": "off", "attributes": {}},
        ],
    })
    out = await load_tools(homeassistant)["ha_list_entities"](domain="light")
    assert [e["entity_id"] for e in out] == ["light.bed", "light.kitchen"]
    assert out[1]["friendly_name"] == "Kitchen"


async def test_get_state(monkeypatch):
    patch_http(monkeypatch, homeassistant, {
        "/api/states/sensor.temp": {"entity_id": "sensor.temp", "state": "21",
                                    "attributes": {"unit": "C"}, "last_changed": "t", "last_updated": "t"},
    })
    out = await load_tools(homeassistant)["ha_get_state"](entity_id="sensor.temp")
    assert out["state"] == "21" and out["attributes"]["unit"] == "C"


async def test_call_service_counts_changes(monkeypatch):
    patch_http(monkeypatch, homeassistant, {
        "/api/services/light/turn_on": [{"entity_id": "light.kitchen"}, {"entity_id": "light.bed"}],
    })
    out = await load_tools(homeassistant)["ha_call_service"](
        domain="light", service="turn_on", entity_id="light.kitchen")
    assert out["called"] == "light.turn_on" and out["changed_states"] == 2


async def test_list_areas_resolves_names(monkeypatch):
    def template_route(request):
        # First call lists area ids; subsequent calls resolve a name.
        return "['kitchen', 'bedroom']" if b"areas()" in request.content else "Bedroom"
    patch_http(monkeypatch, homeassistant, {"/api/template": template_route})
    out = await load_tools(homeassistant)["ha_list_areas"]()
    assert {a["area_id"] for a in out} == {"kitchen", "bedroom"}
    assert out[0]["name"] == "Bedroom"


async def test_list_automations_only_automations(monkeypatch):
    patch_http(monkeypatch, homeassistant, {
        "/api/states": [
            {"entity_id": "light.kitchen", "state": "on", "attributes": {}},
            {"entity_id": "automation.feed", "state": "on",
             "attributes": {"id": "171", "friendly_name": "Feed", "last_triggered": "t"}},
        ],
    })
    out = await load_tools(homeassistant)["ha_list_automations"]()
    assert out == [{"entity_id": "automation.feed", "automation_id": "171",
                    "alias": "Feed", "state": "on", "last_triggered": "t"}]


async def test_save_automation_creates_with_new_id(monkeypatch):
    posted = {}

    def route(request):
        if request.method == "GET":
            return (404, {"message": "Resource not found"})
        posted["body"] = json.loads(request.content)
        posted["path"] = request.url.path
        return {"result": "ok"}

    patch_http(monkeypatch, homeassistant, {"/api/config/automation/config/*": route})
    cfg = {"alias": "Ping", "triggers": [], "actions": []}
    out = await load_tools(homeassistant)["ha_save_automation"](automation=cfg)
    assert out["created"] and posted["body"] == cfg
    assert posted["path"] == f"/api/config/automation/config/{out['automation_id']}"


async def test_save_automation_refuses_id_collision_on_create(monkeypatch):
    patch_http(monkeypatch, homeassistant, {
        "/api/config/automation/config/*": {"alias": "Existing"},
    })
    with pytest.raises(ValueError, match="already exists"):
        await load_tools(homeassistant)["ha_save_automation"](automation={"alias": "New"})


async def test_save_automation_surfaces_validation_error(monkeypatch):
    patch_http(monkeypatch, homeassistant, {
        "/api/config/automation/config/171": (400, {"message": "Message malformed: bad trigger"}),
    })
    with pytest.raises(ValueError, match="bad trigger"):
        await load_tools(homeassistant)["ha_save_automation"](
            automation={"alias": "X"}, automation_id="171")


async def test_save_automation_requires_alias(monkeypatch):
    with pytest.raises(ValueError, match="alias"):
        await load_tools(homeassistant)["ha_save_automation"](automation={"triggers": []})


async def test_get_and_delete_automation(monkeypatch):
    patch_http(monkeypatch, homeassistant, {
        "/api/config/automation/config/171": lambda r: (
            {"result": "ok"} if r.method == "DELETE" else {"id": "171", "alias": "Feed"}),
    })
    tools = load_tools(homeassistant)
    assert (await tools["ha_get_automation"](automation_id="171"))["alias"] == "Feed"
    assert await tools["ha_delete_automation"](automation_id="171") == {"deleted": "171"}


# --- scripts ---

async def test_list_scripts_derives_id_from_entity(monkeypatch):
    patch_http(monkeypatch, homeassistant, {
        "/api/states": [
            {"entity_id": "automation.feed", "state": "on", "attributes": {"id": "171"}},
            {"entity_id": "script.announce", "state": "off",
             "attributes": {"friendly_name": "Announce", "last_triggered": "t"}},
        ],
    })
    out = await load_tools(homeassistant)["ha_list_scripts"]()
    assert out == [{"entity_id": "script.announce", "script_id": "announce",
                    "alias": "Announce", "state": "off", "last_triggered": "t"}]


async def test_get_script(monkeypatch):
    patch_http(monkeypatch, homeassistant, {
        "/api/config/script/config/announce": {"alias": "Announce", "sequence": []},
    })
    tools = load_tools(homeassistant)
    assert (await tools["ha_get_script"](script_id="announce"))["alias"] == "Announce"
    with pytest.raises(ValueError, match="no script config"):
        await tools["ha_get_script"](script_id="gone")


async def test_script_id_rejects_path_characters():
    with pytest.raises(ValueError, match="invalid script_id"):
        await load_tools(homeassistant)["ha_get_script"](script_id="../automation/config/1")


def _script_route(exists: bool, posted: dict):
    def route(request):
        if request.method == "GET":
            return {"alias": "Old", "sequence": []} if exists else (404, {"message": "Resource not found"})
        posted["body"] = json.loads(request.content)
        posted["path"] = request.url.path
        return {"result": "ok"}
    return route


@pytest.mark.parametrize("exists, create", [(True, False), (False, True)])
async def test_save_script_replaces_or_creates(monkeypatch, exists, create):
    posted = {}
    patch_http(monkeypatch, homeassistant, {"/api/config/script/config/*": _script_route(exists, posted)})
    cfg = {"alias": "Announce", "sequence": [{"action": "tts.cloud_say"}]}
    out = await load_tools(homeassistant)["ha_save_script"](script_id="announce", script=cfg, create=create)
    assert out == {"script_id": "announce", "alias": "Announce", "created": create}
    assert posted == {"body": cfg, "path": "/api/config/script/config/announce"}


@pytest.mark.parametrize("exists, create, error", [
    (True, True, "already exists"),
    (False, False, "create=True"),
])
async def test_save_script_refuses_wrong_mode(monkeypatch, exists, create, error):
    posted = {}
    patch_http(monkeypatch, homeassistant, {"/api/config/script/config/*": _script_route(exists, posted)})
    with pytest.raises(ValueError, match=error):
        await load_tools(homeassistant)["ha_save_script"](
            script_id="announce", script={"sequence": [{}]}, create=create)
    assert not posted


async def test_save_script_surfaces_validation_error(monkeypatch):
    patch_http(monkeypatch, homeassistant, {
        "/api/config/script/config/announce": lambda r: (
            {"sequence": []} if r.method == "GET" else (400, {"message": "Message malformed: bad action"})),
    })
    with pytest.raises(ValueError, match="bad action"):
        await load_tools(homeassistant)["ha_save_script"](script_id="announce", script={"sequence": [{}]})


async def test_save_script_requires_sequence():
    with pytest.raises(ValueError, match="sequence"):
        await load_tools(homeassistant)["ha_save_script"](script_id="announce", script={"alias": "X"})


# --- automation audit / export ---

DOOR = "binary_sensor.test_door"


def _states(**extra):
    base = {
        DOOR: {"entity_id": DOOR, "state": "unavailable", "last_changed": "t0",
               "attributes": {"device_class": "door"}},
        "binary_sensor.test_presence": {"entity_id": "binary_sensor.test_presence", "state": "off",
                              "attributes": {"device_class": "occupancy"}},
        "light.test": {"entity_id": "light.test", "state": "off", "attributes": {}},
    }
    base.update(extra)
    return base


def test_lint_flags_device_trigger():
    cfg = {"triggers": [{"trigger": "device", "device_id": "abc", "domain": "binary_sensor"}]}
    assert [f["rule"] for f in homeassistant._lint_automation(cfg, {})] == ["device_trigger"]


@pytest.mark.parametrize("trigger, flagged", [
    ({"trigger": "state", "entity_id": DOOR, "from": "off", "to": "on"}, True),
    # Legacy platform key and list-form from are covered too.
    ({"platform": "state", "entity_id": [DOOR], "from": ["off"], "to": "on"}, True),
    ({"trigger": "state", "entity_id": DOOR, "to": "on"}, False),
    # from: null means "any state" in HA.
    ({"trigger": "state", "entity_id": DOOR, "from": None, "to": "on"}, False),
    ({"trigger": "state", "entity_id": DOOR, "from": ["off", "unavailable"], "to": "on"}, False),
    # Presence sensors often want the from: guard on purpose.
    ({"trigger": "state", "entity_id": "binary_sensor.test_presence", "from": "off", "to": "on"}, False),
])
def test_lint_from_state_only_on_opening_sensors(trigger, flagged):
    findings = homeassistant._lint_automation({"triggers": [trigger]}, _states())
    assert (["from_state_misses_recovery"] if flagged else []) == [f["rule"] for f in findings]


def test_collect_refs_skips_templates_and_notification_action_ids():
    refs = {"entities": set(), "devices": set(), "areas": set(), "services": set()}
    homeassistant._collect_refs({
        "triggers": [{"trigger": "state", "entity_id": [DOOR, "{{ x }}"]}],
        "actions": [
            {"action": "light.turn_on", "target": {"area_id": ["test_area"], "entity_id": "light.test"}},
            {"action": "notify.test_target", "data": {"data": {"actions": [{"action": "SOME_ACTION_ID"}]}}},
            {"service": "{{ svc }}", "device_id": "dev1"},
        ],
    }, refs)
    assert refs == {
        "entities": {DOOR, "light.test"},
        "devices": {"dev1"},
        "areas": {"test_area"},
        "services": {"light.turn_on", "notify.test_target"},
    }


def _auto(aid, state="on", last="t"):
    return {"automation_id": aid, "alias": f"A{aid}", "state": state, "last_triggered": last}


def test_audit_reference_checks_and_ordering():
    configs = {
        "1": {"triggers": [{"trigger": "state", "entity_id": DOOR, "to": "on"}],
              "actions": [{"action": "light.turn_on", "target": {"area_id": "gone_area", "entity_id": "light.gone"}},
                          {"action": "notify.gone_target"},
                          {"device_id": "dead"}]},
        "2": None,
        "3": {"triggers": [{"trigger": "state", "entity_id": "light.gone"}]},
        "4": {"triggers": []},
    }
    autos = [_auto("1"), _auto("2", state="unavailable"), _auto("3", state="off"), _auto("4", last=None)]
    findings = homeassistant._audit(autos, configs, _states(), {"light.turn_on"}, {"test_area"}, {"dead"})
    got = [(f["automation_id"], f["severity"], f["rule"]) for f in findings]
    assert got == [
        ("1", "error", "missing_area"),
        ("1", "error", "missing_device"),
        ("1", "error", "missing_entity"),
        ("1", "error", "missing_service"),
        ("2", "error", "config_invalid"),
        ("1", "warning", "unavailable_entity"),
        # Disabled automations get only the info finding, not their broken refs.
        ("3", "info", "disabled"),
        ("4", "info", "never_triggered"),
    ]


async def test_audit_tool_end_to_end(monkeypatch):
    posted = {}

    def template(request):
        posted.update(json.loads(request.content))
        return json.dumps({"areas": ["test_area"], "missing_devices": [],
                           "device_entities": [["dev1", ["sensor.test_battery", "binary_sensor.test_contact"]]]})

    patch_http(monkeypatch, homeassistant, {
        "/api/states": list(_states(**{"automation.test": {
            "entity_id": "automation.test", "state": "on",
            "attributes": {"id": "171", "friendly_name": "Test lights", "last_triggered": None}}}).values()),
        "/api/config/automation/config/171": {
            "id": "171", "alias": "Test lights",
            "triggers": [{"trigger": "state", "entity_id": DOOR, "from": "off", "to": "on"},
                         {"trigger": "device", "device_id": "dev1", "domain": "binary_sensor"}],
            "actions": [{"action": "light.turn_on", "target": {"area_id": "test_area"}}]},
        "/api/services": [{"domain": "light", "services": {"turn_on": {}}}],
        "/api/template": template,
    })
    tool = load_tools(homeassistant)["ha_audit_automations"]
    out = await tool()
    assert posted["variables"] == {"dev_ids": ["dev1"]}
    assert out["automations_checked"] == 1
    assert out["summary"] == {"error": 0, "warning": 3, "info": 1}
    assert {f["rule"] for f in out["findings"]} == {
        "device_trigger", "from_state_misses_recovery", "unavailable_entity", "never_triggered"}
    device = next(f for f in out["findings"] if f["rule"] == "device_trigger")
    assert device["suggested_entities"] == ["binary_sensor.test_contact"]
    assert (await tool(include_info=False))["summary"]["info"] == 0


async def test_audit_and_export_unknown_id(monkeypatch):
    patch_http(monkeypatch, homeassistant, {"/api/states": []})
    tools = load_tools(homeassistant)
    for name in ("ha_audit_automations", "ha_export_automations"):
        with pytest.raises(ValueError, match="no automation"):
            await tools[name](automation_id="999")


async def test_export_returns_configs_and_skips_unloadable(monkeypatch):
    patch_http(monkeypatch, homeassistant, {
        "/api/states": [
            {"entity_id": "automation.a", "state": "on", "attributes": {"id": "1", "friendly_name": "A"}},
            {"entity_id": "automation.b", "state": "unavailable", "attributes": {"id": "2", "friendly_name": "B"}},
            {"entity_id": "automation.yaml_only", "state": "on", "attributes": {"friendly_name": "No id"}},
        ],
        "/api/config/automation/config/1": {"id": "1", "alias": "A"},
        "/api/config/automation/config/2": (404, {"message": "Resource not found"}),
    })
    out = await load_tools(homeassistant)["ha_export_automations"]()
    assert out == {"count": 1, "automations": [{"id": "1", "alias": "A"}],
                   "skipped": [{"automation_id": "2", "alias": "B"}]}


def _fake_ws(monkeypatch, existing, calls):
    """Stub _ws_session so ha_create_input_boolean runs without a socket."""
    @contextlib.asynccontextmanager
    async def session():
        async def call(payload):
            calls.append(payload)
            if payload["type"] == "input_boolean/list":
                return existing
            return {"id": payload["name"].lower().replace(" ", "_"), "name": payload["name"]}
        yield call
    monkeypatch.setattr(homeassistant, "_ws_session", session)


async def test_create_input_boolean(monkeypatch):
    calls = []
    _fake_ws(monkeypatch, [{"id": "guest_mode", "name": "Guest Mode"}], calls)
    out = await load_tools(homeassistant)["ha_create_input_boolean"](
        name=" Manual Color Lamp ", icon="mdi:palette")
    assert out == {"entity_id": "input_boolean.manual_color_lamp", "name": "Manual Color Lamp", "created": True}
    assert calls[1] == {"type": "input_boolean/create", "name": "Manual Color Lamp",
                        "initial": False, "icon": "mdi:palette"}


async def test_create_input_boolean_refuses_duplicate_and_blank(monkeypatch):
    calls = []
    _fake_ws(monkeypatch, [{"id": "guest_mode", "name": "Guest Mode"}], calls)
    tool = load_tools(homeassistant)["ha_create_input_boolean"]
    with pytest.raises(ValueError, match="already exists"):
        await tool(name="guest mode")
    with pytest.raises(ValueError, match="needs a name"):
        await tool(name="  ")
    assert [c["type"] for c in calls] == ["input_boolean/list"]


_DEVICES = [
    {"id": "d-dead-zb", "name": "Old Bulb", "area_id": "kitchen", "config_entries": ["e-mqtt"],
     "primary_config_entry": "e-mqtt"},
    {"id": "d-live-zb", "name": "Live Bulb", "area_id": "kitchen", "config_entries": ["e-mqtt"],
     "primary_config_entry": "e-mqtt"},
    {"id": "d-dead-esp", "name": "Esp Bulb", "name_by_user": "Lamp", "area_id": None,
     "config_entries": ["e-esp1"], "primary_config_entry": "e-esp1"},
    {"id": "d-dead-shared", "name": "Hub Child", "config_entries": ["e-hub"], "primary_config_entry": "e-hub"},
    {"id": "d-live-shared", "name": "Hub Other", "config_entries": ["e-hub"], "primary_config_entry": "e-hub"},
    {"id": "d-no-entities", "name": "BT Adapter", "area_id": "kitchen", "config_entries": ["e-mqtt"],
     "primary_config_entry": "e-mqtt"},
]
_ENTITIES = [
    {"entity_id": "light.old", "device_id": "d-dead-zb"},
    {"entity_id": "sensor.old_disabled", "device_id": "d-dead-zb", "disabled_by": "user"},
    {"entity_id": "light.live", "device_id": "d-live-zb"},
    {"entity_id": "light.esp", "device_id": "d-dead-esp"},
    {"entity_id": "light.hub_child", "device_id": "d-dead-shared"},
    {"entity_id": "light.hub_other", "device_id": "d-live-shared"},
    {"entity_id": "climate.gone", "device_id": None, "platform": "smartir"},
    {"entity_id": "sensor.offline_template", "device_id": None, "platform": "template"},
]
_ENTRIES = [
    {"entry_id": "e-mqtt", "domain": "mqtt", "title": "MQTT", "state": "loaded", "supports_remove_device": True},
    {"entry_id": "e-esp1", "domain": "esphome", "title": "esp", "state": "loaded", "supports_remove_device": False},
    {"entry_id": "e-hub", "domain": "hub", "title": "Hub", "state": "loaded", "supports_remove_device": False},
]
_STATES = [
    {"entity_id": "light.old", "state": "unavailable", "attributes": {}},
    {"entity_id": "light.live", "state": "on", "attributes": {}},
    {"entity_id": "light.esp", "state": "unavailable", "attributes": {"restored": True}},
    {"entity_id": "light.hub_child", "state": "unavailable", "attributes": {}},
    {"entity_id": "light.hub_other", "state": "on", "attributes": {}},
    {"entity_id": "sensor.offline_template", "state": "unavailable", "attributes": {}},
]


def _fake_registry_ws(monkeypatch, calls):
    @contextlib.asynccontextmanager
    async def session():
        async def call(payload):
            calls.append(payload)
            return {
                "config/device_registry/list": _DEVICES,
                "config/entity_registry/list": _ENTITIES,
                "config_entries/get": _ENTRIES,
                "get_states": _STATES,
            }.get(payload["type"])
        yield call
    monkeypatch.setattr(homeassistant, "_ws_session", session)


async def test_list_devices_dead_only(monkeypatch):
    _fake_registry_ws(monkeypatch, [])
    out = await load_tools(homeassistant)["ha_list_devices"]()
    by_id = {d["device_id"]: d for d in out["devices"]}
    assert set(by_id) == {"d-dead-zb", "d-dead-esp", "d-dead-shared"}
    assert by_id["d-dead-zb"]["removable"] is True and by_id["d-dead-zb"]["integration"] == "mqtt"
    # Disabled entities don't count towards liveness and aren't listed.
    assert by_id["d-dead-zb"]["entities"] == [{"entity_id": "light.old", "state": "unavailable"}]
    assert by_id["d-dead-esp"]["name"] == "Lamp"
    assert by_id["d-dead-esp"]["entities"][0]["state"] == "restored"
    assert by_id["d-dead-esp"]["sole_device_of_entry"] is True
    assert by_id["d-dead-shared"]["sole_device_of_entry"] is False
    # Orphans: no device and nothing providing the entity. An unavailable but
    # provided entity (template) is not an orphan.
    assert out["orphan_entities"] == [{"entity_id": "climate.gone", "platform": "smartir", "state": None}]


async def test_list_devices_all_and_area(monkeypatch):
    _fake_registry_ws(monkeypatch, [])
    tool = load_tools(homeassistant)["ha_list_devices"]
    out = await tool(dead_only=False, area_id="kitchen")
    # Devices with no entities aren't "dead", but show up when listing everything.
    assert sorted(d["device_id"] for d in out["devices"]) == ["d-dead-zb", "d-live-zb", "d-no-entities"]
    assert out["orphan_entities"] == []


async def test_remove_device_via_registry(monkeypatch):
    calls = []
    _fake_registry_ws(monkeypatch, calls)
    out = await load_tools(homeassistant)["ha_remove_device"](device_id="d-dead-zb")
    assert out == {"device_id": "d-dead-zb", "name": "Old Bulb", "removed": True, "via": "device_registry"}
    assert calls[-1] == {"type": "config/device_registry/remove", "device_id": "d-dead-zb"}


async def test_remove_device_refusals(monkeypatch):
    calls = []
    _fake_registry_ws(monkeypatch, calls)
    tool = load_tools(homeassistant)["ha_remove_device"]
    with pytest.raises(ValueError, match="live entities"):
        await tool(device_id="d-live-zb")
    with pytest.raises(ValueError, match="no device"):
        await tool(device_id="nope")
    with pytest.raises(ValueError, match="delete_config_entry=True"):
        await tool(device_id="d-dead-esp")
    with pytest.raises(ValueError, match="other devices"):
        await tool(device_id="d-dead-shared", delete_config_entry=True)
    assert not any(c["type"] == "config/device_registry/remove" for c in calls)


async def test_remove_device_via_config_entry(monkeypatch):
    _fake_registry_ws(monkeypatch, [])
    seen = {}

    def route(request):
        seen["method"], seen["path"] = request.method, request.url.path
        return {"require_restart": False}
    patch_http(monkeypatch, homeassistant, {"/api/config/config_entries/entry/e-esp1": route})
    out = await load_tools(homeassistant)["ha_remove_device"](device_id="d-dead-esp", delete_config_entry=True)
    assert seen == {"method": "DELETE", "path": "/api/config/config_entries/entry/e-esp1"}
    assert out["via"] == "config_entry" and out["config_entry"]["domain"] == "esphome"


async def test_remove_entity(monkeypatch):
    calls = []
    _fake_registry_ws(monkeypatch, calls)
    tool = load_tools(homeassistant)["ha_remove_entity"]
    assert await tool(entity_id="climate.gone") == {"entity_id": "climate.gone", "removed": True}
    assert calls[-1] == {"type": "config/entity_registry/remove", "entity_id": "climate.gone"}
    assert (await tool(entity_id="light.esp"))["removed"] is True  # restored = not provided
    with pytest.raises(ValueError, match="provided by a loaded integration"):
        await tool(entity_id="sensor.offline_template")


def _fake_dashboard_ws(monkeypatch, calls, dashboards=(), configs=None, yaml_paths=()):
    """Stub _ws_session for the dashboard tools. `configs` maps url_path -> stored config;
    a known path missing from it has no stored config (strategy dashboard)."""
    configs = configs or {}
    known = {"", *(d["url_path"] for d in dashboards)}

    @contextlib.asynccontextmanager
    async def session():
        async def call(payload):
            calls.append(payload)
            kind, path = payload["type"], payload.get("url_path", "")
            if kind == "lovelace/dashboards/list":
                return list(dashboards)
            if kind == "lovelace/resources":
                return [{"id": "1", "url": "/local/card.js", "type": "module"}]
            if kind == "lovelace/config":
                if path not in known:
                    raise homeassistant.HAWebSocketError(
                        f"Home Assistant rejected lovelace/config: Unknown config specified: {path}",
                        code="config_not_found")
                if path not in configs:
                    raise homeassistant.HAWebSocketError(
                        "Home Assistant rejected lovelace/config: No config found.", code="config_not_found")
                return configs[path]
            if kind == "lovelace/config/save" and path in yaml_paths:
                raise homeassistant.HAWebSocketError(
                    "Home Assistant rejected lovelace/config/save: Not supported", code="error")
            return None
        yield call
    monkeypatch.setattr(homeassistant, "_ws_session", session)


async def test_list_dashboards_includes_default_and_resources(monkeypatch):
    calls = []
    _fake_dashboard_ws(monkeypatch, calls, dashboards=[
        {"id": "b", "url_path": "zz-dash", "title": "Z", "mode": "yaml"},
        {"id": "a", "url_path": "aa-dash", "title": "A", "mode": "storage", "show_in_sidebar": False},
    ])
    out = await load_tools(homeassistant)["ha_list_dashboards"]()
    assert [d["url_path"] for d in out["dashboards"]] == ["", "aa-dash", "zz-dash"]
    assert out["dashboards"][0]["default"] is True
    assert out["dashboards"][1]["show_in_sidebar"] is False
    assert out["dashboards"][2]["mode"] == "yaml"
    assert out["resources"] == [{"url": "/local/card.js", "type": "module"}]


async def test_get_dashboard_stored_generated_and_unknown(monkeypatch):
    calls = []
    cfg = {"views": [{"title": "Home", "cards": []}]}
    _fake_dashboard_ws(monkeypatch, calls, dashboards=[{"url_path": "my-dash"}], configs={"my-dash": cfg})
    tool = load_tools(homeassistant)["ha_get_dashboard"]
    assert await tool(url_path="my-dash") == {"url_path": "my-dash", "generated": False, "config": cfg}
    assert calls[-1] == {"type": "lovelace/config", "force": False, "url_path": "my-dash"}
    # The default dashboard has no stored config here: it's auto-generated.
    assert await tool() == {"url_path": "", "generated": True, "config": None}
    assert "url_path" not in calls[-1]
    with pytest.raises(ValueError, match="Unknown config"):
        await tool(url_path="nope-dash")


async def test_save_dashboard_returns_previous(monkeypatch):
    calls = []
    old = {"views": [{"title": "Old"}]}
    new = {"views": [{"title": "New"}]}
    _fake_dashboard_ws(monkeypatch, calls, dashboards=[{"url_path": "my-dash"}], configs={"my-dash": old})
    out = await load_tools(homeassistant)["ha_save_dashboard"](url_path="my-dash", config=new)
    assert out == {"url_path": "my-dash", "created": False, "saved": True, "previous": old}
    assert calls[-1] == {"type": "lovelace/config/save", "config": new, "url_path": "my-dash"}


async def test_save_dashboard_create(monkeypatch):
    calls = []
    _fake_dashboard_ws(monkeypatch, calls, dashboards=[{"url_path": "my-dash"}])
    out = await load_tools(homeassistant)["ha_save_dashboard"](
        url_path="new-dash", config={"views": []}, create=True, title=" New ", icon="mdi:home")
    assert out["created"] is True and out["previous"] is None
    assert calls[1] == {"type": "lovelace/dashboards/create", "url_path": "new-dash", "title": "New",
                        "show_in_sidebar": True, "icon": "mdi:home"}
    assert calls[2]["type"] == "lovelace/config/save"


async def test_save_dashboard_refusals(monkeypatch):
    calls = []
    _fake_dashboard_ws(monkeypatch, calls, dashboards=[{"url_path": "my-dash"}, {"url_path": "yaml-dash"}],
                       configs={"yaml-dash": {"views": []}}, yaml_paths={"yaml-dash"})
    tool = load_tools(homeassistant)["ha_save_dashboard"]
    with pytest.raises(ValueError, match="views"):
        await tool(url_path="my-dash", config={"title": "x"})
    with pytest.raises(ValueError, match="needs a title"):
        await tool(url_path="new-dash", config={"views": []}, create=True)
    with pytest.raises(ValueError, match="hyphen"):
        await tool(url_path="newdash", config={"views": []}, create=True, title="T")
    with pytest.raises(ValueError, match="already exists"):
        await tool(url_path="my-dash", config={"views": []}, create=True, title="T")
    with pytest.raises(ValueError, match="YAML-mode"):
        await tool(url_path="yaml-dash", config={"views": []})
    assert not any(c["type"] == "lovelace/dashboards/create" for c in calls)


class _FakeSocket:
    def __init__(self, replies):
        self.replies, self.sent = list(replies), []

    async def recv(self):
        return json.dumps(self.replies.pop(0))

    async def send(self, data):
        self.sent.append(json.loads(data))


def _patch_connect(monkeypatch, socket):
    @contextlib.asynccontextmanager
    async def connect(url, **kwargs):
        socket.url, socket.kwargs = url, kwargs
        yield socket
    monkeypatch.setattr(homeassistant, "connect", connect)
    monkeypatch.setattr(homeassistant.config.homeassistant, "url", "https://ha.example/")
    monkeypatch.setattr(homeassistant.config.homeassistant, "token", "tok")


async def test_ws_session_authenticates_and_matches_reply_ids(monkeypatch):
    sock = _FakeSocket([
        {"type": "auth_required"}, {"type": "auth_ok"},
        {"id": 99, "type": "result", "success": True, "result": "stray"},
        {"id": 1, "type": "result", "success": True, "result": [1]},
    ])
    _patch_connect(monkeypatch, sock)
    async with homeassistant._ws_session() as call:
        assert await call({"type": "x/list"}) == [1]
    assert sock.url == "wss://ha.example/api/websocket"
    assert sock.kwargs["max_size"] >= 16 * 1024 * 1024  # big registry dumps
    assert sock.sent == [{"type": "auth", "access_token": "tok"}, {"id": 1, "type": "x/list"}]


async def test_ws_session_errors(monkeypatch):
    _patch_connect(monkeypatch, _FakeSocket([{"type": "auth_required"}, {"type": "auth_invalid", "message": "bad"}]))
    with pytest.raises(ValueError, match="rejected the token"):
        async with homeassistant._ws_session():
            pass
    _patch_connect(monkeypatch, _FakeSocket([{"type": "hi"}]))
    with pytest.raises(ValueError, match="unexpected greeting"):
        async with homeassistant._ws_session():
            pass
    _patch_connect(monkeypatch, _FakeSocket([
        {"type": "auth_required"}, {"type": "auth_ok"},
        {"id": 1, "type": "result", "success": False, "error": {"message": "nope"}}]))
    with pytest.raises(ValueError, match="rejected x/create: nope"):
        async with homeassistant._ws_session() as call:
            await call({"type": "x/create"})


async def test_history_runs_durations_summary_and_filters(monkeypatch):
    seen = {}

    def route(request):
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return [[
            {"entity_id": "binary_sensor.bed_left", "state": "on", "last_changed": "2026-11-04T04:00:00+00:00"},
            {"state": "off", "last_changed": "2026-11-04T05:00:00+00:00"},
            {"state": "on", "last_changed": "2026-11-04T05:00:12+00:00"},
            {"state": "off", "last_changed": "2026-11-04T06:00:00+00:00"},
            {"state": "on", "last_changed": "2026-11-04T06:00:40+00:00"},
        ]]
    patch_http(monkeypatch, homeassistant, {"/api/history/period/*": route})
    out = await load_tools(homeassistant)["ha_history"](
        entity_ids=["binary_sensor.bed_left", "binary_sensor.gone"],
        start="2026-11-04T04:00:00Z", end="2026-11-04T07:00:00Z", state="off", max_seconds=30)
    assert seen["path"] == "/api/history/period/2026-11-04T04:00:00+00:00"
    assert seen["params"]["filter_entity_id"] == "binary_sensor.bed_left,binary_sensor.gone"
    assert "minimal_response" in seen["params"] and "no_attributes" in seen["params"]
    e = out["entities"]["binary_sensor.bed_left"]
    # Only the 12 s dropout is off and <= 30 s; the 40 s one is filtered out.
    assert e["runs"] == [{"state": "off", "at": "2026-11-04T05:00:00+00:00", "seconds": 12.0}]
    # Summary ignores the clipped first/last runs.
    assert e["summary"]["off"] == {"count": 2, "min_s": 12.0, "max_s": 40.0, "median_s": 40.0}
    assert e["summary"]["on"]["count"] == 1 and e["summary"]["on"]["max_s"] == 3588.0
    assert out["no_history"] == ["binary_sensor.gone"]


async def test_history_rejects_bad_windows():
    tools = load_tools(homeassistant)
    with pytest.raises(ValueError):
        await tools["ha_history"](entity_ids=[])
    with pytest.raises(ValueError):
        await tools["ha_history"](entity_ids=["a.b"], start="2026-11-04T05:00:00Z", end="2026-11-04T04:00:00Z")
    with pytest.raises(ValueError):
        await tools["ha_history"](entity_ids=["a.b"], hours=24 * 30)


async def test_history_numeric_sensor_gets_value_summary_and_bounds(monkeypatch):
    patch_http(monkeypatch, homeassistant, {"/api/history/period/*": [[
        {"entity_id": "sensor.lux", "state": "5.0", "last_changed": "2026-11-04T04:00:00+00:00"},
        {"state": "1.0", "last_changed": "2026-11-04T04:00:10+00:00"},
        {"state": "3.0", "last_changed": "2026-11-04T04:00:40+00:00"},
        {"state": "unavailable", "last_changed": "2026-11-04T04:00:50+00:00"},
        {"state": "9.0", "last_changed": "2026-11-04T04:00:52+00:00"},
        {"state": "2.0", "last_changed": "2026-11-04T04:01:00+00:00"},
    ]]})
    out = await load_tools(homeassistant)["ha_history"](
        entity_ids=["sensor.lux"], start="2026-11-04T04:00:00Z", end="2026-11-04T04:02:00Z", below=4)
    e = out["entities"]["sensor.lux"]
    # Unclipped readings: 1.0 for 30 s, 3.0 for 10 s, 9.0 for 8 s. Not one bucket per value.
    assert e["summary"]["numeric"] == {
        "readings": 3, "min": 1.0, "max": 9.0, "median": 3.0,
        "time_weighted_mean": round((1 * 30 + 3 * 10 + 9 * 8) / 48, 3)}
    assert e["summary"]["unavailable"]["count"] == 1
    assert "1.0" not in e["summary"]
    assert [r["state"] for r in e["runs"]] == ["1.0", "3.0", "2.0"]


async def test_history_mostly_text_states_keep_per_state_buckets(monkeypatch):
    patch_http(monkeypatch, homeassistant, {"/api/history/period/*": [[
        {"entity_id": "binary_sensor.x", "state": "on", "last_changed": "2026-11-04T04:00:00+00:00"},
        {"state": "off", "last_changed": "2026-11-04T04:00:10+00:00"},
        {"state": "on", "last_changed": "2026-11-04T04:00:20+00:00"},
        {"state": "off", "last_changed": "2026-11-04T04:00:30+00:00"},
    ]]})
    out = await load_tools(homeassistant)["ha_history"](
        entity_ids=["binary_sensor.x"], start="2026-11-04T04:00:00Z", end="2026-11-04T04:01:00Z")
    s = out["entities"]["binary_sensor.x"]["summary"]
    assert "numeric" not in s and s["off"]["count"] == 1 and s["on"]["count"] == 1


async def test_refusals_reach_the_caller(monkeypatch):
    """The MCP SDK hides the text of unexpected exceptions; refusals and HA's
    own rejections must be ToolErrors so the caller sees why."""
    from mcp.server.mcpserver.exceptions import ToolError
    _fake_registry_ws(monkeypatch, [])
    with pytest.raises(ToolError, match="live entities"):
        await load_tools(homeassistant)["ha_remove_device"](device_id="d-live-zb")
    assert issubclass(homeassistant.HAWebSocketError, ToolError)


async def test_delete_dashboard(monkeypatch):
    calls = []
    cfg = {"views": [{"title": "T"}]}
    _fake_dashboard_ws(monkeypatch, calls, dashboards=[
        {"id": "d1", "url_path": "old-dash", "title": "Old", "mode": "storage"},
        {"id": "d2", "url_path": "yaml-dash", "mode": "yaml"},
    ], configs={"old-dash": cfg})
    tool = load_tools(homeassistant)["ha_delete_dashboard"]
    out = await tool(url_path="old-dash")
    assert out == {"url_path": "old-dash", "title": "Old", "deleted": True, "previous": cfg}
    assert calls[-1] == {"type": "lovelace/dashboards/delete", "dashboard_id": "d1"}
    for path, match in (("", "default"), ("nope-dash", "no dashboard"), ("yaml-dash", "YAML")):
        with pytest.raises(homeassistant.HARefusal, match=match):
            await tool(url_path=path)
