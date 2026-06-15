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
