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
