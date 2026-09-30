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
