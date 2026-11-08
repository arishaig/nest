"""Floor-plan engine, model, rendering and tools on a small synthetic plan.

The plan (300 x 200): an outer wall, and an interior wall at x 146..154 with a
window gap (y 20..60, two pane lines) and a door gap (y 110..150, swing drawn
to the left). Left room is x 8..146, right room x 154..292.
"""
import base64
import contextlib
import io
import json

import httpx
import numpy as np
import pytest
from PIL import Image, ImageDraw

from helpers import load_tools
from nest_mcp.floorplan import engine, model as M, render as R
from nest_mcp.tools import floorplan, homeassistant

W, H = 300, 200
LEFT, RIGHT = (60, 130), (230, 130)


def plan_image() -> Image.Image:
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W - 1, 7], fill=0)
    d.rectangle([0, H - 8, W - 1, H - 1], fill=0)
    d.rectangle([0, 0, 7, H - 1], fill=0)
    d.rectangle([W - 8, 0, W - 1, H - 1], fill=0)
    for y0, y1 in ((8, 20), (60, 110), (150, H - 8)):
        d.rectangle([146, y0, 153, y1 - 1], fill=0)
    d.line([(148, 20), (148, 59)], fill=30)      # window panes
    d.line([(151, 20), (151, 59)], fill=30)
    d.line([(146, 110), (106, 110)], fill=120)   # door leaf + swing
    d.arc([106, 70, 186, 150], 90, 180, fill=120)
    return img


def plan_png() -> bytes:
    buf = io.BytesIO()
    plan_image().save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def gray():
    return np.array(plan_image())


# --- engine ---------------------------------------------------------------------

def test_walls_and_gaps(gray):
    raw, sealed = engine.detect_walls(gray)
    assert raw[100, 2] and raw[100, 150]                 # outer + interior wall
    assert not raw[40, 148]                              # pane lines are not walls
    assert sealed[40, 150] and sealed[130, 150]          # both gaps sealed
    gaps = engine.find_gaps(gray, raw, sealed)
    assert [(g["id"], g["guess"]) for g in gaps] == [("gap-1", "window"), ("gap-2", "door")]
    x0, y0, x1, y1 = gaps[1]["rect"]
    assert x0 >= 146 and x1 <= 154 and y0 >= 108 and y1 <= 152


def test_morphology_matches_box_semantics():
    a = np.zeros((9, 9), bool)
    a[4, 4] = True
    assert engine._window(a, 3, 3, False).sum() == 9
    assert not engine._window(a, 3, 3, True).any()
    assert engine._close(np.pad(np.ones((1, 3), bool), ((0, 0), (0, 4))) | np.pad(np.ones((1, 2), bool), ((0, 0), (5, 0))),
                         1, 4).all()


def test_light_stays_behind_closed_door_and_passes_open_one(gray):
    raw, sealed = engine.detect_walls(gray)
    closed = engine.soft_cast(sealed, *LEFT, radius=6, samples=6, blur=2)
    assert closed[LEFT[1], LEFT[0]] > 200 and closed[RIGHT[1], RIGHT[0]] == 0
    door = engine.draw_mask(gray.shape, rects=[[146, 110, 154, 150]]) & sealed & ~raw
    opened = engine.soft_cast(sealed & ~door, *LEFT, radius=6, samples=6, blur=2)
    assert opened[140, 200] > 100                         # in line with the doorway
    assert not opened[sealed & ~door].any()               # never paints on walls


def test_partial_transmission_scales_the_extra_light(gray):
    raw, sealed = engine.detect_walls(gray)
    window = engine.draw_mask(gray.shape, rects=[[146, 20, 154, 60]]) & sealed & ~raw
    full = engine.lit(sealed & ~window, 60, 40, radius=4, samples=4, blur=0)
    part = engine.lit(sealed, 60, 40, [(0.5, window)], radius=4, samples=4, blur=0)
    assert full[40, 220] == 255 and 120 <= part[40, 220] <= 130
    assert part[40, 60] == 255


def test_soft_cast_rejects_light_inside_wall(gray):
    _, sealed = engine.detect_walls(gray)
    with pytest.raises(ValueError, match="inside a wall"):
        engine.soft_cast(sealed, 150, 100)


def test_mask_data_uri_is_white_alpha_png():
    m = np.zeros((30, 60), np.uint8)
    m[:, 30:] = 255
    uri = engine.mask_data_uri(m, scale=3)
    img = Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))
    assert img.mode == "LA" and img.size == (20, 10)
    assert img.getchannel("L").getextrema() == (255, 255)
    assert img.getchannel("A").getpixel((19, 5)) == 248 and img.getchannel("A").getpixel((0, 5)) == 0


def test_differs_ignores_noise():
    a = np.zeros((40, 40), np.uint8)
    b = a.copy()
    b[:5, :5] = 255
    assert engine.differs(a, b) == 0 and engine.differs(a, b, min_pixels=10) == 25


# --- model ----------------------------------------------------------------------

def make_model():
    m = M.new_model("/local/plan.png", (W, H))
    m["light"].update({"radius": 6, "samples": 4, "blur": 2})
    m["fixtures"] = {
        "left": {"entity": "light.left", "kind": "light", "x": LEFT[0], "y": LEFT[1]},
        "right": {"entity": "light.right", "kind": "light", "x": RIGHT[0], "y": RIGHT[1]},
        "fan": {"entity": "fan.f", "kind": "fan", "x": 30, "y": 30},
    }
    m["openings"] = {"door": {"kind": "door", "rect": [146, 110, 154, 150], "state": "sensor",
                              "sensor": "binary_sensor.door"}}
    return m


def test_geometry_sensor_variants_and_blockers(gray):
    m = make_model()
    geo = M.Geometry(m, gray)
    variants, warnings = M.mask_variants(geo, "left")
    assert warnings == [] and [v for v, _ in variants] == [{"binary_sensor.door": False},
                                                          {"binary_sensor.door": True}]
    assert variants[0][1][140, 200] == 0 and variants[1][1][140, 200] > 100
    m["blockers"] = {"screen": {"rect": [100, 100, 140, 160], "fixtures": ["left"]}}
    geo = M.Geometry(m, gray)
    b, _ = geo.barrier_for("left", {})
    b_right, _ = geo.barrier_for("right", {})
    assert b[120, 120] and not b_right[120, 120]


def test_sensor_far_away_gets_no_variant(gray):
    m = make_model()
    m["fixtures"]["left"].update({"x": 30, "y": 40})
    m["blockers"] = {"wall": {"line": [8, 70, 146, 70], "fixtures": []}}
    variants, _ = M.mask_variants(M.Geometry(m, gray), "left")
    assert [v for v, _ in variants] == [{}]


def test_closed_opening_kinds_transmit(gray):
    m = make_model()
    m["openings"] = {"win": {"kind": "window", "rect": [146, 20, 154, 60], "state": "closed"},
                     "arch": {"kind": "door", "rect": [146, 110, 154, 150], "state": "open"},
                     "glass": {"kind": "glass_door", "line": [8, 90, 146, 90], "state": "closed",
                               "transmission": 0.25}}
    geo = M.Geometry(m, gray)
    b, partial = geo.barrier_for("left", {})
    assert sorted(t for t, _ in partial) == [0.25, 0.7]
    assert not b[130, 150]                                  # open door carved
    assert b[90, 50]                                        # glass line is a wall when closed
    assert geo.sensors() == []


def test_too_many_sensors_warns(gray):
    m = make_model()
    m["openings"] = {
        "a": {"kind": "door", "rect": [146, 110, 154, 150], "state": "sensor", "sensor": "binary_sensor.a"},
        "b": {"kind": "door", "rect": [146, 20, 154, 60], "state": "sensor", "sensor": "binary_sensor.b"},
        "c": {"kind": "door", "line": [8, 170, 100, 170], "state": "sensor", "sensor": "binary_sensor.c"},
    }
    m["fixtures"]["left"].update({"x": 60, "y": 100})
    variants, warnings = M.mask_variants(M.Geometry(m, gray), "left")
    assert len(variants) == 4 and "ignoring" in warnings[0]


def test_fixture_key_tracks_geometry_not_calibration():
    m = make_model()
    k = M.fixture_key(m, "left")
    m["fixtures"]["left"]["glow"] = {"size": 0.5}
    assert M.fixture_key(m, "left") == k
    m["fixtures"]["left"]["x"] = 61
    assert M.fixture_key(m, "left") != k
    k2 = M.fixture_key(m, "left")
    m["blockers"] = {"x": {"rect": [1, 1, 2, 2], "fixtures": ["right"]}}
    assert M.fixture_key(m, "left") == k2 != M.fixture_key(m, "right")


def test_glow_defaults_and_shape():
    m = make_model()
    g = M.glow(m, m["fixtures"]["left"])
    assert g["whiten"] == 0.6 and g["default_rgb"] == [255, 225, 185] and g["color_k"] == 0.5
    full = M.glow_shape(g, 1000)
    assert full["radius_px"] == pytest.approx(340) and 0.5 < full["alpha"] < 0.6
    g.update(dim_reach=0.5, dim_boost=2)
    dim = M.glow_shape(g, 1000, brightness=0.0, rgb=(255, 0, 0), color_temp=False)
    assert dim["radius_px"] == pytest.approx(70) and dim["rgb"] == [255, 0, 0]


def test_validation_helpers():
    m = make_model()
    with pytest.raises(M.ModelError):
        M.check_id("Bad Id", "fixture")
    with pytest.raises(M.ModelError):
        M.check_entity("lightkitchen")
    with pytest.raises(M.ModelError, match="outside"):
        M.check_point(m, W, 0, "x")
    with pytest.raises(M.ModelError, match="exactly one"):
        M.check_shape(m, None, None)
    with pytest.raises(M.ModelError, match="four"):
        M.check_shape(m, [1, 2], None)
    with pytest.raises(M.ModelError, match="x1 > x0"):
        M.check_shape(m, [5, 5, 4, 9], None)
    with pytest.raises(M.ModelError, match="end"):
        M.check_shape(m, None, [0, 0, W + 5, 0])
    assert M.check_shape(m, None, [0, 0, 10, 10]) == {"line": [0, 0, 10, 10]}


def test_history_is_capped():
    m = make_model()
    for i in range(M.HISTORY_LIMIT + 3):
        M.push_history(m, {"n": i}, f"step {i}", "t")
    assert len(m["history"]) == M.HISTORY_LIMIT and m["history"][-1]["note"] == f"step {M.HISTORY_LIMIT + 2}"
    assert "history" not in M.snapshot(m)


# --- render ---------------------------------------------------------------------

@pytest.mark.parametrize(("anchor", "pad", "offset", "scale", "expected"), [
    ("center", 6, (0, 0), 1.25, "translate(-50%, -50%) scale(1.25)"),
    ("se", 6, (0, 46), 1, "translate(6px, calc(6px + 46px))"),
    ("nw", 6, (0, 0), 1, "translate(calc(-100% - 6px), calc(-100% - 6px))"),
    ("w", 4, (2, 0), 1, "translate(calc(-100% - 4px + 2px), -50%)"),
])
def test_icon_transform(anchor, pad, offset, scale, expected):
    assert R.icon_transform(anchor, pad, offset, scale) == expected


def test_glow_template_carries_parameters_and_gate():
    g = {**M.GLOW_DEFAULTS, "size": 0.8, "whiten": 0.75, "default_rgb": [255, 215, 150], "color_k": 0.5,
         "dim_reach": 0.9}
    tpl = R.glow_template(g, "10.00%", "20.00%", 1.5, {"binary_sensor.b": True, "binary_sensor.a": False})
    assert "set size = 0.8" in tpl and "set whiten = 0.75" in tpl and "[255, 215, 150]" in tpl
    assert "set dimk = 0.9" in tpl and "at 10.00% 20.00%" in tpl and "(rx * 1.5000)" in tpl
    assert ("is_state(e, 'on') and not is_state('binary_sensor.a', 'on') "
            "and is_state('binary_sensor.b', 'on') else 0") in tpl


def test_build_elements_reuses_masks_and_card_roundtrip(gray):
    m = make_model()
    geo = M.Geometry(m, gray)
    els, stats = R.build_elements(m, geo, {}, workers=2)          # process pool path
    assert sorted(stats["regenerated"]) == ["left", "right"]
    serial, _ = R.build_elements(m, geo, {})
    assert serial == els
    glows = [e for e in els if e["type"] == "image"]
    icons = [e for e in els if e["type"] == "state-icon"]
    assert len(glows) == 4 and len(icons) == 3
    assert all(e["style"]["mask-image"] == "var(--fp-mask)" for e in glows)
    fan = next(e for e in icons if e["entity"] == "fan.f")
    assert fan["icon"] == "mdi:fan" and "fp-spin" in fan["card_mod"]["style"]
    card = R.build_card(m, els)
    reuse = R.previous_masks(card)
    assert R.stale_fixtures(m, reuse) == []
    again, stats2 = R.build_elements(m, None, reuse)       # no geometry needed
    assert stats2["regenerated"] == [] and sorted(stats2["reused"]) == ["left", "right"]
    assert again == els
    with pytest.raises(RuntimeError):
        R.build_elements(m, None, {})
    m["fixtures"]["left"]["x"] = 70
    assert R.stale_fixtures(m, reuse) == ["left"]


def test_place_card_replaces_marked_card_or_adds_view():
    cfg = {"views": [{"title": "Home", "cards": [{"type": "markdown"}]}]}
    R.place_card(cfg, {"type": "picture-elements", M.CARD_MARKER: True, "v": 1}, "Plan")
    assert cfg["views"][1]["path"] == "floor-plan" and cfg["views"][1]["title"] == "Plan"
    R.place_card(cfg, {"type": "picture-elements", M.CARD_MARKER: True, "v": 2}, "Plan")
    assert len(cfg["views"]) == 2
    assert cfg["views"][1]["sections"][0]["cards"][0]["v"] == 2
    nested = {"views": [{"cards": [{"type": "vertical-stack", "cards": [{M.CARD_MARKER: True}]}]}]}
    R.place_card(nested, {"new": True}, "")
    assert nested["views"][0]["cards"][0]["cards"][0] == {"new": True}


def test_preview_png(gray):
    m = make_model()
    m["blockers"] = {"b": {"line": [20, 20, 40, 20], "fixtures": []}}
    m["openings"]["w"] = {"kind": "window", "rect": [146, 20, 154, 60], "state": "closed"}
    png = R.preview(m, M.Geometry(m, gray), "left", 1.0, show_gaps=True, max_width=200)
    img = Image.open(io.BytesIO(png))
    assert img.size == (200, round(H * 200 / W))


# --- tools ----------------------------------------------------------------------

class FakeHA:
    """Dashboards over the WebSocket API, plan images and service calls over REST."""

    def __init__(self, configs=None, dashboards=()):
        self.configs = configs if configs is not None else {}
        self.dashboards = list(dashboards)
        self.ws = []
        self.services = []
        self.states = {}

    def install(self, monkeypatch):
        @contextlib.asynccontextmanager
        async def session():
            async def call(payload):
                self.ws.append(payload)
                kind, path = payload["type"], payload.get("url_path", "")
                if kind == "lovelace/dashboards/list":
                    return self.dashboards
                if kind == "lovelace/dashboards/create":
                    self.dashboards.append({"url_path": path})
                    return None
                if kind == "lovelace/config":
                    if path not in self.configs:
                        raise homeassistant.HAWebSocketError("No config found.", code="config_not_found")
                    return json.loads(json.dumps(self.configs[path]))
                if kind == "lovelace/config/save":
                    self.configs[path] = json.loads(json.dumps(payload["config"]))
                return None
            yield call

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/local/plan.png":
                return httpx.Response(200, content=plan_png())
            if path == "/local/plan.svg":
                svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}">'
                       f'<rect x="0" y="0" width="{W}" height="8" fill="black"/></svg>')
                return httpx.Response(200, content=svg.encode())
            if path == "/api/states":
                return httpx.Response(200, json=list(self.states.values()))
            if path.startswith("/api/services/"):
                self.services.append((path.removeprefix("/api/services/"), json.loads(request.content)))
                return httpx.Response(200, json=[])
            return httpx.Response(404)

        def fake_make_client(base_url, **kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url)

        monkeypatch.setattr(homeassistant, "_ws_session", session)
        monkeypatch.setattr(floorplan, "make_client", fake_make_client)
        floorplan._raster_cache.clear()
        return self


@pytest.fixture
def tools():
    return load_tools(floorplan)


@pytest.fixture
def ha(monkeypatch):
    return FakeHA(configs={"plan-dash": {"views": [{"title": "Home", "cards": []}]}},
                  dashboards=[{"url_path": "plan-dash"}]).install(monkeypatch)


async def _start(tools, ha):
    summary, image = await tools["floorplan_create"](url_path="plan-dash", image="/local/plan.png")
    model = ha.configs["plan-dash"][M.MODEL_KEY]
    model["light"].update({"radius": 6, "samples": 4, "blur": 2})   # fast masks for tests
    return summary, image


def _card(ha):
    holder, i = R.find_card(ha.configs["plan-dash"])
    return holder[i]


async def test_create_adds_view_and_reports_gaps(tools, ha):
    summary, image = await _start(tools, ha)
    assert summary["size"] == [W, H] and [g["guess"] for g in summary["gaps"]] == ["window", "door"]
    assert image.to_image_content().mime_type == "image/png"
    cfg = ha.configs["plan-dash"]
    assert cfg["views"][1]["path"] == "floor-plan" and _card(ha)["elements"] == []
    with pytest.raises(floorplan.FloorplanError, match="already has a floor plan"):
        await tools["floorplan_create"](url_path="plan-dash", image="/local/plan.png")


async def test_create_new_dashboard_from_svg(tools, ha):
    pytest.importorskip("cairosvg", exc_type=OSError)    # needs the system cairo library
    summary, _ = await tools["floorplan_create"](url_path="new-plan", image="/local/plan.svg",
                                                 title="Plan", create_dashboard=True)
    assert summary["size"] == [W, H] and {"url_path": "new-plan"} in ha.dashboards
    with pytest.raises(floorplan.FloorplanError, match="already exists"):
        await tools["floorplan_create"](url_path="new-plan", image="/local/plan.svg", title="P",
                                        create_dashboard=True)
    with pytest.raises(floorplan.FloorplanError, match="hyphen"):
        await tools["floorplan_create"](url_path="plan", image="/local/plan.svg", create_dashboard=True)


async def test_create_refusals(tools, ha):
    ha.dashboards.append({"url_path": "auto-dash"})
    with pytest.raises(floorplan.FloorplanError, match="auto-generated"):
        await tools["floorplan_create"](url_path="auto-dash", image="/local/plan.png")
    with pytest.raises(floorplan.FloorplanError, match="must be a path"):
        await tools["floorplan_create"](url_path="plan-dash", image="plan.png")
    with pytest.raises(floorplan.FloorplanError, match="no floor plan"):
        await tools["floorplan_get"](url_path="auto-dash")


async def test_fixture_lifecycle(tools, ha):
    await _start(tools, ha)
    out = await tools["floorplan_set_fixture"](url_path="plan-dash", fixture_id="lamp",
                                               entity="light.lamp", x=LEFT[0], y=LEFT[1],
                                               icon_x=20, icon_y=20, icon_anchor="se", icon_scale=1)
    assert out["masks_regenerated"] == ["lamp"] and out["fixture"]["icon_at"] == [20, 20]
    els = _card(ha)["elements"]
    assert [e["type"] for e in els] == ["image", "state-icon"]
    assert els[1]["style"]["transform"] == "translate(6px, 6px)"
    out = await tools["floorplan_set_fixture"](url_path="plan-dash", fixture_id="fan",
                                               entity="fan.ceiling", x=200, y=60)
    assert out["masks_regenerated"] == [] and out["fixture"]["kind"] == "fan"
    out = await tools["floorplan_set_fixture"](url_path="plan-dash", fixture_id="lamp", x=70)
    assert out["masks_regenerated"] == ["lamp"]
    with pytest.raises(floorplan.FloorplanError, match="inside a wall"):
        await tools["floorplan_set_fixture"](url_path="plan-dash", fixture_id="lamp", x=150, y=100)
    with pytest.raises(floorplan.FloorplanError, match="needs entity"):
        await tools["floorplan_set_fixture"](url_path="plan-dash", fixture_id="new")
    with pytest.raises(floorplan.FloorplanError, match="icon_anchor"):
        await tools["floorplan_set_fixture"](url_path="plan-dash", fixture_id="lamp", icon_anchor="up")
    with pytest.raises(M.ModelError, match="outside"):
        await tools["floorplan_set_fixture"](url_path="plan-dash", fixture_id="lamp", y=999)
    got = await tools["floorplan_get"](url_path="plan-dash", include_gaps=True)
    assert [f["id"] for f in got["fixtures"]] == ["fan", "lamp"] and len(got["gaps"]) == 2
    out = await tools["floorplan_remove_fixture"](url_path="plan-dash", fixture_id="fan")
    assert [e["entity"] for e in _card(ha)["elements"]] == ["light.lamp", "light.lamp"]
    with pytest.raises(floorplan.FloorplanError, match="no fixture"):
        await tools["floorplan_remove_fixture"](url_path="plan-dash", fixture_id="fan")


async def test_openings_blockers_and_sensor_layers(tools, ha):
    await _start(tools, ha)
    await tools["floorplan_set_fixture"](url_path="plan-dash", fixture_id="lamp", entity="light.lamp",
                                         x=LEFT[0], y=LEFT[1])
    out = await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="door", from_gap="gap-2",
                                               state="sensor", sensor="binary_sensor.door")
    assert out["opening"]["door"]["kind"] == "door" and out["masks_regenerated"] == ["lamp"]
    glows = [e for e in _card(ha)["elements"] if e["type"] == "image"]
    assert len(glows) == 2 and "not is_state('binary_sensor.door', 'on')" in glows[0]["card_mod"]["style"]
    await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win", rect=[146, 20, 154, 60],
                                         kind="window")
    await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win", transmission=0.5)
    model = ha.configs["plan-dash"][M.MODEL_KEY]
    assert model["openings"]["win"] == {"rect": [146, 20, 154, 60], "kind": "window", "state": "closed",
                                        "transmission": 0.5}
    with pytest.raises(floorplan.FloorplanError, match="needs a sensor"):
        await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="x", line=[8, 90, 146, 90],
                                             state="sensor")
    with pytest.raises(floorplan.FloorplanError, match="no gap"):
        await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="x", from_gap="gap-9")
    with pytest.raises(floorplan.FloorplanError, match="needs from_gap"):
        await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="x")
    with pytest.raises(floorplan.FloorplanError, match="kind"):
        await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="x", rect=[1, 1, 2, 2],
                                             kind="portal")
    with pytest.raises(floorplan.FloorplanError, match="state"):
        await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="x", rect=[1, 1, 2, 2],
                                             state="ajar")
    with pytest.raises(floorplan.FloorplanError, match="between 0 and 1"):
        await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="x", rect=[1, 1, 2, 2],
                                             transmission=2)
    await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win", remove=True)
    with pytest.raises(floorplan.FloorplanError, match="no opening"):
        await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win", remove=True)
    out = await tools["floorplan_set_blocker"](url_path="plan-dash", blocker_id="island",
                                               rect=[90, 100, 120, 160], fixtures=["lamp"])
    assert out["blocker"]["island"]["fixtures"] == ["lamp"] and out["masks_regenerated"] == ["lamp"]
    await tools["floorplan_set_blocker"](url_path="plan-dash", blocker_id="island", fixtures=[])
    with pytest.raises(floorplan.FloorplanError, match="unknown fixtures"):
        await tools["floorplan_set_blocker"](url_path="plan-dash", blocker_id="island", fixtures=["nope"])
    with pytest.raises(floorplan.FloorplanError, match="needs rect"):
        await tools["floorplan_set_blocker"](url_path="plan-dash", blocker_id="new")
    await tools["floorplan_set_blocker"](url_path="plan-dash", blocker_id="island", remove=True)
    with pytest.raises(floorplan.FloorplanError, match="no blocker"):
        await tools["floorplan_set_blocker"](url_path="plan-dash", blocker_id="island", remove=True)


async def test_calibrate_settings_undo_rebuild_preview(tools, ha):
    await _start(tools, ha)
    for fid, (x, y) in (("a", LEFT), ("b", (40, 40))):
        await tools["floorplan_set_fixture"](url_path="plan-dash", fixture_id=fid, entity="light.hall",
                                             x=x, y=y)
    out = await tools["floorplan_calibrate"](url_path="plan-dash", entity="light.hall", size=0.8,
                                             default_rgb=[255, 215, 150])
    assert out["masks_regenerated"] == [] and set(out["fixtures"]) == {"a", "b"}
    assert out["fixtures"]["a"]["after"] == {"size": 0.8, "default_rgb": [255, 215, 150]}
    assert "set size = 0.8" in _card(ha)["elements"][0]["card_mod"]["style"]
    out = await tools["floorplan_calibrate"](url_path="plan-dash", fixture_id="a", reset=True, strength=1.1)
    assert out["fixtures"]["a"]["after"] == {"strength": 1.1}
    for bad, match in (({"size": 0}, "> 0"), ({"whiten": 2}, "between"), ({"default_rgb": [1, 2]}, "r, g, b")):
        with pytest.raises(floorplan.FloorplanError, match=match):
            await tools["floorplan_calibrate"](url_path="plan-dash", fixture_id="a", **bad)
    with pytest.raises(floorplan.FloorplanError, match="exactly one"):
        await tools["floorplan_calibrate"](url_path="plan-dash")
    with pytest.raises(floorplan.FloorplanError, match="nothing to change"):
        await tools["floorplan_calibrate"](url_path="plan-dash", fixture_id="a")
    with pytest.raises(floorplan.FloorplanError, match="no fixture"):
        await tools["floorplan_calibrate"](url_path="plan-dash", entity="light.none", size=1)

    out = await tools["floorplan_undo"](url_path="plan-dash")
    assert out["undone"].startswith("calibrated a")
    model = ha.configs["plan-dash"][M.MODEL_KEY]
    assert model["fixtures"]["a"]["glow"] == {"size": 0.8, "default_rgb": [255, 215, 150]}

    out = await tools["floorplan_settings"](url_path="plan-dash", light={"mask_scale": 2},
                                            transmission={"window": 0.5})
    assert sorted(out["masks_regenerated"]) == ["a", "b"] and out["settings"]["transmission"]["window"] == 0.5
    with pytest.raises(floorplan.FloorplanError, match="unknown light settings"):
        await tools["floorplan_settings"](url_path="plan-dash", light={"glow": 1})
    out = await tools["floorplan_rebuild"](url_path="plan-dash")
    assert out["masks_regenerated"] == [] and out["masks_reused"] == 2
    out = await tools["floorplan_rebuild"](url_path="plan-dash", force=True)
    assert sorted(out["masks_regenerated"]) == ["a", "b"]

    meta, image = await tools["floorplan_preview"](url_path="plan-dash", fixture_id="a", brightness_pct=40,
                                                   show_gaps=True)
    assert meta["fixture"] == "a" and image.to_image_content().data
    with pytest.raises(floorplan.FloorplanError, match="no fixture"):
        await tools["floorplan_preview"](url_path="plan-dash", fixture_id="zz")
    got = await tools["floorplan_get"](url_path="plan-dash")
    assert got["history"][-1]["note"] == "settings changed" and got["calibration_session"] is False

    while (await tools["floorplan_get"](url_path="plan-dash"))["history"]:
        await tools["floorplan_undo"](url_path="plan-dash")
    with pytest.raises(floorplan.FloorplanError, match="nothing to undo"):
        await tools["floorplan_undo"](url_path="plan-dash")


async def test_calibration_session_snapshots_and_restores(tools, ha):
    await _start(tools, ha)
    await tools["floorplan_set_fixture"](url_path="plan-dash", fixture_id="lamp", entity="light.lamp",
                                         x=LEFT[0], y=LEFT[1])
    await tools["floorplan_set_fixture"](url_path="plan-dash", fixture_id="sw", entity="switch.hall",
                                         x=RIGHT[0], y=RIGHT[1])
    ha.states = {
        "light.lamp": {"entity_id": "light.lamp", "state": "on", "attributes": {
            "brightness": 128, "color_mode": "xy", "xy_color": [0.7, 0.3], "effect": "fireplace",
            "effect_list": ["none", "fireplace"]}},
        "switch.hall": {"entity_id": "switch.hall", "state": "off", "attributes": {}},
    }
    tool = tools["floorplan_calibration"]
    with pytest.raises(floorplan.FloorplanError, match="no calibration session"):
        await tool(url_path="plan-dash", action="show", entity="light.lamp")
    out = await tool(url_path="plan-dash", action="start", pause_automations=["automation.motion"])
    assert out["snapshot"] == {"light.lamp": "on", "switch.hall": "off"}
    assert ("automation/turn_off", {"entity_id": ["automation.motion"]}) in ha.services
    assert ha.configs["plan-dash"][M.MODEL_KEY]["session"]["paused"] == ["automation.motion"]
    with pytest.raises(floorplan.FloorplanError, match="already running"):
        await tool(url_path="plan-dash", action="start")
    ha.services.clear()
    out = await tool(url_path="plan-dash", action="show", entity="light.lamp", brightness_pct=40,
                     color_temp_kelvin=2700)
    assert out == {"showing": "light.lamp", "brightness_pct": 40, "color_temp_kelvin": 2700, "effect": "none"}
    assert ha.services[0] == ("switch/turn_off", {"entity_id": "switch.hall"})
    ha.states["light.lamp"]["attributes"]["supported_color_modes"] = ["color_temp", "xy"]
    await tool(url_path="plan-dash", action="show", entity="light.lamp", brightness_pct=10)
    assert ha.services[-1] == ("light/turn_on", {"entity_id": "light.lamp", "brightness_pct": 10,
                                                 "color_temp_kelvin": 2700, "effect": "none"})
    await tool(url_path="plan-dash", action="show", entity="light.lamp", rgb_color=[255, 0, 0])
    assert ha.services[-1][1]["rgb_color"] == [255, 0, 0] and "color_temp_kelvin" not in ha.services[-1][1]
    await tool(url_path="plan-dash", action="show", entity="switch.hall")
    assert ha.services[-1] == ("switch/turn_on", {"entity_id": "switch.hall"})
    with pytest.raises(floorplan.FloorplanError, match="not a light"):
        await tool(url_path="plan-dash", action="show", entity="light.other")
    ha.services.clear()
    out = await tool(url_path="plan-dash", action="end")
    assert ("light/turn_on", {"entity_id": "light.lamp", "brightness": 128, "xy_color": [0.7, 0.3],
                              "effect": "fireplace"}) in ha.services
    assert ("switch/turn_off", {"entity_id": "switch.hall"}) in ha.services
    assert ("automation/turn_on", {"entity_id": ["automation.motion"]}) in ha.services
    assert ha.configs["plan-dash"][M.MODEL_KEY]["session"] is None
    with pytest.raises(floorplan.FloorplanError, match="start, show or end"):
        await tool(url_path="plan-dash", action="pause")


def test_restore_payload_colour_temp():
    service, data = floorplan._restore_payload({"entity_id": "light.x", "state": "on", "attributes": {
        "brightness": 255, "color_mode": "color_temp", "color_temp_kelvin": 2857, "effect": "none"}})
    assert service == "light.turn_on" and data == {"entity_id": "light.x", "brightness": 255,
                                                    "color_temp_kelvin": 2857}


def test_svg_size_and_bitmap_resize():
    assert floorplan._svg_size(b'<svg viewBox="0 0 10.4 20">') == (10, 20)
    assert floorplan._svg_size(b'<svg width="30" height="40">') == (30, 40)
    assert floorplan._svg_size(b"<svg>") is None
    with pytest.raises(floorplan.FloorplanError, match="viewBox"):
        floorplan._rasterize(b"<svg></svg>", None)
    rgba = Image.new("RGBA", (20, 10), (0, 0, 0, 0))
    buf = io.BytesIO()
    rgba.save(buf, "PNG")
    out = floorplan._rasterize(buf.getvalue(), (40, 20))
    assert out.shape == (20, 40) and out.min() == 255       # transparent -> white


async def test_fetch_external_url_without_token(monkeypatch):
    seen = {}

    def fake_make_client(base_url, **kwargs):
        seen.update(kwargs, base=base_url)
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x")),
                                 base_url=base_url)

    monkeypatch.setattr(floorplan, "make_client", fake_make_client)
    assert await floorplan._fetch("https://example.com/plan.png") == b"x"
    assert "headers" not in seen


def test_validate_normalises_and_rejects():
    m = make_model()
    m["blockers"] = {"b": {"rect": [1, 1, 5, 5], "fixtures": ["left"]}}
    out = M.validate(m)
    assert out["fixtures"]["left"]["x"] == LEFT[0] and out["blockers"]["b"]["fixtures"] == ["left"]
    assert out["walls"] == M.WALL_DEFAULTS and out["light"]["samples"] == 4
    bad_cases = [
        ([], "object"), ({"image": "x"}, "image and size"),
        ({**m, "walls": {"nope": 1}}, "unknown walls"),
        ({**m, "fixtures": {"a": {"entity": "light.a", "kind": "lamp", "x": 1, "y": 1}}}, "kind"),
        ({**m, "fixtures": {"a": {"entity": "light.a", "kind": "light", "x": 1, "y": 1, "icon_anchor": "up"}}},
         "icon_anchor"),
        ({**m, "fixtures": {"a": {"entity": "light.a", "kind": "light", "x": 1, "y": 1, "glow": {"hue": 1}}}},
         "glow"),
        ({**m, "openings": {"o": {"kind": "portal", "rect": [1, 1, 2, 2]}}}, "kind or state"),
        ({**m, "openings": {"o": {"kind": "door", "rect": [1, 1, 2, 2], "state": "sensor"}}}, "sensor"),
        ({**m, "blockers": {"b": {"rect": [1, 1, 2, 2], "fixtures": ["ghost"]}}}, "unknown fixtures"),
    ]
    for bad, match in bad_cases:
        with pytest.raises(M.ModelError, match=match):
            M.validate(bad)


async def test_import_adopts_hand_built_card_and_roundtrips(tools, ha):
    ha.configs["plan-dash"] = {"views": [{"type": "sections", "sections": [{"cards": [
        {"type": "markdown"}, {"type": "picture-elements", "image": "/local/old.svg", "elements": [{"x": 1}]}]}]}]}
    m = make_model()
    out = await tools["floorplan_import"](url_path="plan-dash", model=m, adopt_card=True)
    assert out["adopted_card"] is True and out["fixtures"] == 3 and sorted(out["masks_regenerated"]) == ["left", "right"]
    cards = ha.configs["plan-dash"]["views"][0]["sections"][0]["cards"]
    assert len(ha.configs["plan-dash"]["views"]) == 1 and cards[1]["image"] == "/local/plan.png"
    raw = (await tools["floorplan_get"](url_path="plan-dash", raw=True))["model"]
    assert raw["fixtures"] == m["fixtures"] and "history" not in raw
    out = await tools["floorplan_import"](url_path="plan-dash", model=raw)
    assert out["masks_regenerated"] == [] and out["adopted_card"] is False
    assert (await tools["floorplan_undo"](url_path="plan-dash"))["undone"] == "imported model"
    with pytest.raises(floorplan.FloorplanError, match="image and size"):
        await tools["floorplan_import"](url_path="plan-dash", model={"fixtures": {}})
    with pytest.raises(floorplan.FloorplanError, match="no stored config"):
        await tools["floorplan_import"](url_path="ghost-dash", model=m)
    ha.configs["bare-dash"] = {"views": [{"cards": []}]}
    with pytest.raises(floorplan.FloorplanError, match="no picture-elements"):
        await tools["floorplan_import"](url_path="bare-dash", model=m, adopt_card=True)


# --- sunlight --------------------------------------------------------------------

WINDOW = [146, 20, 154, 60]          # the plan's window gap; facing "w" looks out into the left room


def sun_model(**extra):
    m = M.new_model("/local/plan.png", (W, H))
    m["light"].update({"radius": 6, "samples": 4, "blur": 2})
    m["openings"] = {"win": {"kind": "window", "rect": WINDOW, "state": "closed", "facing": "w"}}
    m.update(extra)
    return M.validate(m)


def test_window_reach_covers_its_room_only(gray):
    geo = M.Geometry(sun_model(), gray)
    barrier, _ = geo.barrier_for("", {})
    fr = M.window_frame(geo.model["openings"]["win"])
    reach = engine.window_reach(barrier, fr["mx"], fr["my"], fr["tx"], fr["ty"], fr["half"], *fr["normal_in"])
    assert reach[130, 230] > 200 and reach[30, 200] > 200        # right room, in view of the window
    assert reach[100, 60] == 0                                    # left room is behind the wall
    assert not reach[barrier].any()                               # never paints on walls


def test_window_reach_needs_floor_inside(gray):
    barrier = np.ones_like(gray, bool)
    with pytest.raises(ValueError, match="no open floor"):
        engine.window_reach(barrier, 150, 40, 0, 1, 20, 1, 0)


def test_fill_region_is_the_enclosed_area(gray):
    barrier, _ = M.Geometry(sun_model(), gray).barrier_for("", {})
    region = engine.fill_region(barrier, *LEFT)
    assert region[100, 60] > 200 and region[130, 230] == 0
    assert not region[barrier].any()
    with pytest.raises(ValueError, match="inside a wall"):
        engine.fill_region(barrier, 2, 100)


def test_window_frame_checks_the_facing():
    fr = M.window_frame({"rect": WINDOW, "facing": "e"})
    assert (fr["mx"], fr["my"], fr["half"]) == (150, 40, 20) and fr["normal_in"] == (-1, 0)
    assert M.window_frame({"line": [0, 0, 100, 0], "facing": "n"})["half"] == 50
    with pytest.raises(M.ModelError, match="runs along"):
        M.window_frame({"rect": WINDOW, "facing": "n"})           # a vertical wall can't face north
    with pytest.raises(M.ModelError, match="facing must be"):
        M.window_frame({"rect": WINDOW, "facing": "up"})


def test_validate_sun_settings_outdoors_and_overrides():
    m = M.new_model("/local/plan.png", (W, H))
    m["sun"] = {"strength": 0.5}
    m["outdoors"] = {"deck": {"x": 20, "y": 20}}
    m["openings"] = {"edge": {"kind": "open_air", "rect": [0, 20, 8, 60], "facing": "w",
                              "sun": {"height_m": 2.4, "spread": 1}}}
    out = M.validate(m)
    assert out["sun"]["strength"] == 0.5 and out["sun"]["cloud_dim"] == M.SUN_DEFAULTS["cloud_dim"]
    assert out["outdoors"] == {"deck": {"x": 20, "y": 20}}
    assert out["openings"]["edge"]["sun"] == {"height_m": 2.4, "spread": 1}
    for bad, match in (
        ({"sun": {"nope": 1}}, "unknown sun settings"),
        ({"outdoors": {"deck": {"x": 999, "y": 1}}}, "outside the plan"),
        ({"openings": {"d": {"kind": "door", "rect": [1, 1, 5, 5], "facing": "w"}}}, "only windows"),
        ({"openings": {"w": {"kind": "window", "rect": WINDOW, "facing": "w", "sun": {"x": 1}}}},
         "unknown sun overrides"),
    ):
        with pytest.raises(M.ModelError, match=match):
            M.validate({**M.new_model("/local/plan.png", (W, H)), **bad})


def test_sun_masks_and_keys(gray):
    m = sun_model(outdoors={"left": {"x": LEFT[0], "y": LEFT[1]}})
    masks = M.sun_masks(M.Geometry(m, gray))
    assert set(masks) == {"window:win", "outdoor:left"}
    assert set(M.sun_masks(M.Geometry(m, gray), only={"outdoor:left"})) == {"outdoor:left"}
    key = R.sun_key_for(m, "window:win")
    assert key.startswith("sun:window:win:")
    m["openings"]["win"]["facing"] = "e"
    assert R.sun_key_for(m, "window:win") != key                  # the facing is part of the key
    assert R.stale_sun(m, {key: "x"}) == ["window:win", "outdoor:left"]


def test_beam_template_follows_the_sun_and_cloud():
    m = sun_model()
    t = R.beam_template(m, M.sun_windows(m)["win"])
    assert "state_attr('sun.sun', 'azimuth')" in t and "state_attr('weather.forecast_home', 'cloud_coverage')" in t
    assert "1 - 0.45 * cloud / 100" in t and "{% set s = 2.9 %}" in t
    assert "{% set inc = -1 * sin(az * r) - (0) * cos(az * r) %}" in t     # facing w: outward normal (-1, 0)
    assert "stop offset='0' " in t and "@" not in t                          # every placeholder is filled
    # A 40 px window is widened to a 60 px beam and the blur cut to 40 % of its half-width.
    assert "{% set h = 30 %}" in t and "stdDeviation='12.0'" in t


def test_beam_template_overrides_and_north():
    m = sun_model(sun={"north_deg": 90, "px_per_m": 100})
    m["openings"]["win"]["sun"] = {"height_m": 2.4, "spread": 1.0, "blur": 5, "strength": 0.6, "full_until": 0.85}
    t = R.beam_template(m, M.sun_windows(m)["win"])
    assert "- 90 %}" in t and "100 / tan" in t
    assert "{% set s = 1.0 %}" in t and "stdDeviation='5'" in t and "offset='0.85'" in t
    assert "[2.4 * k, 700]" in t and "0.6 * clear" in t


def test_wash_template_fades_with_elevation():
    t = R.wash_template(sun_model())
    assert "opacity: {{ 1 if el > 0 else 0 }}" in t and "rgba(255, 248, 232, {{ a }})" in t


def test_build_elements_puts_sun_first_and_reuses_masks(gray):
    m = sun_model(outdoors={"left": {"x": LEFT[0], "y": LEFT[1]}})
    m["fixtures"] = {"lamp": {"entity": "light.lamp", "kind": "light", "x": LEFT[0], "y": LEFT[1]}}
    geo = M.Geometry(m, gray)
    els, stats = R.build_elements(m, geo, {})
    keys = [e["style"].get("--fp-key", "") for e in els]
    assert keys[0].startswith("sun:outdoor:left") and keys[1].startswith("sun:window:win")
    assert not keys[2].startswith("sun:") and els[-1]["type"] == "state-icon"      # lights, then icons
    assert stats["sun_regenerated"] == ["window:win", "outdoor:left"]
    reuse = R.previous_masks(R.build_card(m, els))
    els2, stats2 = R.build_elements(m, None, reuse)                                # no geometry needed
    assert stats2["sun_regenerated"] == [] and stats2["sun_reused"] == ["window:win", "outdoor:left"]
    assert els2 == els
    with pytest.raises(RuntimeError, match="geometry needed"):
        R.build_elements(m, None, {})


def test_preview_marks_facing_and_outdoors(gray):
    m = sun_model(outdoors={"left": {"x": LEFT[0], "y": LEFT[1]}})
    png = R.preview(m, M.Geometry(m, gray))
    assert Image.open(io.BytesIO(png)).size == (W, H)


def test_lamp_keys_ignore_sun_only_changes():
    m = sun_model()
    m["fixtures"] = {"lamp": {"entity": "light.lamp", "kind": "light", "x": LEFT[0], "y": LEFT[1]}}
    key = M.fixture_key(m, "lamp")
    m["openings"]["win"]["facing"] = "e"
    m["openings"]["win"]["sun"] = {"spread": 1.5}
    assert M.fixture_key(m, "lamp") == key                         # sun-only edits never rebuild lamp masks
    m["openings"]["win"]["state"] = "open"
    assert M.fixture_key(m, "lamp") != key                         # a real geometry change does


async def test_sun_tools_roundtrip(tools, ha):
    await _start(tools, ha)
    await tools["floorplan_set_fixture"](url_path="plan-dash", fixture_id="lamp", entity="light.lamp",
                                         x=RIGHT[0], y=RIGHT[1])
    out = await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win", from_gap="gap-1",
                                               facing="w")
    assert out["opening"]["win"]["facing"] == "w" and out["sun_masks_regenerated"] == ["window:win"]
    assert out["masks_regenerated"] == ["lamp"]                    # the opening itself changed the lamp's walls
    els = [e for e in _card(ha)["elements"] if e["style"].get("--fp-key", "").startswith("sun:")]
    assert len(els) == 1 and "--fp-mask" in els[0]["style"]
    # sun overrides merge; an empty dict clears them
    out = await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win", sun={"spread": 1.5})
    assert out["opening"]["win"]["sun"] == {"spread": 1.5} and "sun_masks_regenerated" not in out
    assert out["masks_regenerated"] == []                          # lamp masks reused
    out = await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win", sun={"blur": 4})
    assert out["opening"]["win"]["sun"] == {"spread": 1.5, "blur": 4}
    out = await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win", sun={})
    assert "sun" not in out["opening"]["win"]
    # an outdoor area adds a wash layer
    out = await tools["floorplan_set_outdoor"](url_path="plan-dash", area_id="deck", x=LEFT[0], y=LEFT[1])
    assert out["outdoors"] == {"deck": {"x": LEFT[0], "y": LEFT[1]}} and out["sun_masks_regenerated"] == ["outdoor:deck"]
    assert any("rgba(255, 248, 232" in e["card_mod"]["style"] for e in _card(ha)["elements"]
               if e["style"].get("--fp-key", "").startswith("sun:outdoor"))
    # settings change the template, not the masks
    out = await tools["floorplan_settings"](url_path="plan-dash", sun={"strength": 0.5, "cloud_dim": 0.2})
    assert out["settings"]["sun"]["strength"] == 0.5 and "sun_masks_regenerated" not in out
    assert "0.5 * clear" in next(e for e in _card(ha)["elements"]
                                 if e["style"].get("--fp-key", "").startswith("sun:window"))["card_mod"]["style"]
    summary = await tools["floorplan_get"](url_path="plan-dash")
    assert summary["outdoors"] == {"deck": {"x": LEFT[0], "y": LEFT[1]}}
    forced = await tools["floorplan_rebuild"](url_path="plan-dash", force=True)
    assert sorted(forced["sun_masks_regenerated"]) == ["outdoor:deck", "window:win"]
    await tools["floorplan_set_outdoor"](url_path="plan-dash", area_id="deck", remove=True)
    await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win", facing="none")
    assert not [e for e in _card(ha)["elements"] if e["style"].get("--fp-key", "").startswith("sun:")]


async def test_sun_tool_refusals(tools, ha):
    await _start(tools, ha)
    await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="door", from_gap="gap-2")
    await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win", from_gap="gap-1")
    for kwargs, match in (
        ({"opening_id": "door", "facing": "w"}, "can have a facing"),
        ({"opening_id": "win", "facing": "up"}, "facing must be"),
        ({"opening_id": "win", "facing": "n"}, "runs along"),
        ({"opening_id": "win", "sun": {"spread": 1}}, "need a facing"),
        ({"opening_id": "win", "facing": "w", "sun": {"bogus": 1}}, "unknown sun overrides"),
    ):
        with pytest.raises(floorplan.FloorplanError, match=match):
            await tools["floorplan_set_opening"](url_path="plan-dash", **kwargs)
    with pytest.raises(floorplan.FloorplanError, match="give a point"):
        await tools["floorplan_set_outdoor"](url_path="plan-dash", area_id="deck")
    with pytest.raises(floorplan.FloorplanError, match="outside the plan"):
        await tools["floorplan_set_outdoor"](url_path="plan-dash", area_id="deck", x=999, y=5)
    with pytest.raises(floorplan.FloorplanError, match="no outdoor area"):
        await tools["floorplan_set_outdoor"](url_path="plan-dash", area_id="deck", remove=True)
    with pytest.raises(floorplan.FloorplanError, match="unknown sun settings"):
        await tools["floorplan_settings"](url_path="plan-dash", sun={"nope": 1})


def test_fade_by_elevation():
    m = sun_model()
    plain = R.beam_template(m, M.sun_windows(m)["win"])
    assert "{% set fade = 1 %}" in plain
    m["openings"]["win"]["sun"] = {"fade_from_deg": 15, "fade_to_deg": 40}
    faded = R.beam_template(m, M.sun_windows(m)["win"])
    assert "{% set fade = [[(40 - el) / (40 - 15), 1] | min, 0] | max %}" in faded and "up * fade *" in faded
    assert M.validate(m)["openings"]["win"]["sun"] == {"fade_from_deg": 15, "fade_to_deg": 40}
    for sun, match in (({"fade_from_deg": 15}, "together"), ({"fade_from_deg": 40, "fade_to_deg": 15}, "fade needs"),
                       ({"fade_from_deg": -5, "fade_to_deg": 20}, "fade needs"),
                       ({"fade_from_deg": 10, "fade_to_deg": 95}, "fade needs")):
        m["openings"]["win"]["sun"] = sun
        with pytest.raises(M.ModelError, match=match):
            M.validate(m)


async def test_fade_tool_refusal_and_merge(tools, ha):
    await _start(tools, ha)
    await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win", from_gap="gap-1", facing="w")
    out = await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win",
                                               sun={"fade_from_deg": 15, "fade_to_deg": 40})
    assert out["opening"]["win"]["sun"] == {"fade_from_deg": 15, "fade_to_deg": 40}
    assert "sun_masks_regenerated" not in out                      # template-only
    out = await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win", sun={"fade_to_deg": 30})
    assert out["opening"]["win"]["sun"] == {"fade_from_deg": 15, "fade_to_deg": 30}   # merges into the pair
    with pytest.raises(floorplan.FloorplanError, match="fade needs"):
        await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win",
                                             sun={"fade_from_deg": 20, "fade_to_deg": 10})
    await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win", sun={})
    with pytest.raises(floorplan.FloorplanError, match="together"):
        await tools["floorplan_set_opening"](url_path="plan-dash", opening_id="win", sun={"fade_to_deg": 30})
