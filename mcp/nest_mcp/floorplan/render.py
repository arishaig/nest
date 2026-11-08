"""Dashboard elements and preview images for a floor-plan model.

Glow layers need the card-mod frontend plugin: its templates follow each light's
state, colour and brightness. Without card-mod the icons still work; the glow
just doesn't render.
"""
import io

import numpy as np
from PIL import Image, ImageDraw

from nest_mcp.floorplan import engine
from nest_mcp.floorplan import model as M

_BLANK = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='1' height='1'/%3E"


def _pct(v: float, total: int) -> str:
    return f"{v / total * 100:.2f}%"


def _gate(assign: dict[str, bool]) -> str:
    return "".join(
        f" and {'' if is_open else 'not '}is_state('{sensor}', 'on')" for sensor, is_open in sorted(assign.items()))


def glow_template(g: dict, cx: str, cy: str, aspect: float, assign: dict[str, bool]) -> str:
    """card_mod template drawing a light's glow from its live state.

    Kelvin-mode colours are blended toward white by `whiten` (HA's kelvin->rgb is
    too orange); brightness drives reach (reach_curve) and intensity
    (intensity_curve); dim_reach/dim_boost only act below 40 % brightness.
    """
    rgb = ", ".join(str(int(v)) for v in g["default_rgb"])
    return (
        "{% set e = config.entity %}"
        f"{{% set c = state_attr(e, 'rgb_color') or [{rgb}] %}}"
        f"{{% set whiten = {g['whiten']} %}}"
        "{% set w = whiten if state_attr(e, 'color_mode') == 'color_temp' else 0 %}"
        "{% set c = [(c[0] + (255 - c[0]) * w) | int, (c[1] + (255 - c[1]) * w) | int, (c[2] + (255 - c[2]) * w) | int] %}"
        "{% set rgb = c | join(', ') %}"
        "{% set lum = (0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]) / 255 %}"
        "{% set b = (state_attr(e, 'brightness') or 255) | int / 255 %}"
        f"{{% set size = {g['size']} %}}{{% set strength = {g['strength']} %}}"
        f"{{% set curve = {g['reach_curve']} %}}{{% set curve_a = {g['intensity_curve']} %}}"
        f"{{% set color_k = {g['color_k']} %}}"
        f"{{% set dimk = {g['dim_reach']} %}}{{% set dimb = {g['dim_boost']} %}}"
        "{% set bb = b ** curve %}{% set ba = b ** curve_a %}"
        "{% set dimf = dimk + (1 - dimk) * ([b / 0.4, 1] | min) %}"
        "{% set dimg = 1 + (dimb - 1) * (1 - ([b / 0.4, 1] | min)) %}"
        "{% set rx = (14 + 20 * bb) * size * dimf %}"
        "{% set a = [(0.25 + 0.35 * ba) * strength * dimg * (lum ** color_k), 0.95] | min %}\n"
        ":host {\n"
        "  transition: opacity 0.4s ease;\n"
        f"  opacity: {{{{ 1 if is_state(e, 'on'){_gate(assign)} else 0 }}}};\n"
        f"  background: radial-gradient(ellipse {{{{ rx | round(1) }}}}% {{{{ (rx * {aspect:.4f}) | round(1) }}}}% "
        f"at {cx} {cy}, rgba({{{{ rgb }}}}, {{{{ a | round(2) }}}}) 0%, "
        "rgba({{ rgb }}, {{ (a * 0.45) | round(2) }}) 45%, rgba({{ rgb }}, 0) 100%);\n"
        "}"
    )


# --- Sunlight -----------------------------------------------------------------
#
# A beam is a window-wide strip that fans out and fades along the direction the
# sunlight travels; the template redraws it from the sun's azimuth/elevation and
# the cloud cover. The static per-window mask (what the window can see) is the
# element's inline mask, so beams stop at walls.

_SUN_COMMON = (
    "{% set az = state_attr('@SUN@', 'azimuth') | float(0) - @NORTH@ %}"
    "{% set el = state_attr('@SUN@', 'elevation') | float(0) %}"
    "{% set cloud = state_attr('@WEATHER@', 'cloud_coverage') | float(0) %}"
    "{% set r = pi / 180 %}"
    "{% set up = [[el / 6, 1] | min, 0] | max %}"
    "{% set clear = 1 - @CLOUD_DIM@ * cloud / 100 %}"
)

_BEAM = (
    "{% set ux = 0 - sin(az * r) %}{% set uy = cos(az * r) %}"
    "{% set inc = @NX@ * sin(az * r) - (@NY@) * cos(az * r) %}"
    "{% set k = @PPM@ / tan(([el, 4] | max) * r) %}{% set d1 = ([@HEIGHT@ * k, @MAXPX@] | min) %}"
    "{% set mx = @MX@ %}{% set my = @MY@ %}{% set tx = @TX@ %}{% set ty = @TY@ %}"
    "{% set h = @HALF@ %}{% set s = @SPREAD@ %}"
    "@FADE@"
    "{% set a = (@STRENGTH@ * clear * up * fade * (0.5 + 0.5 * ([inc, 0] | max))) | round(2) %}"
    "{% set ex = mx + d1 * ux %}{% set ey = my + d1 * uy %}"
    "{% set pts = ((mx - h * tx) | round(1)) ~ ',' ~ ((my - h * ty) | round(1)) ~ ' ' "
    "~ ((mx + h * tx) | round(1)) ~ ',' ~ ((my + h * ty) | round(1)) ~ ' ' "
    "~ ((ex + s * h * tx) | round(1)) ~ ',' ~ ((ey + s * h * ty) | round(1)) ~ ' ' "
    "~ ((ex - s * h * tx) | round(1)) ~ ',' ~ ((ey - s * h * ty) | round(1)) %}\n"
    ":host {\n  transition: opacity 0.4s ease;\n"
    "  opacity: {{ 1 if el > 0 and inc > 0 else 0 }};\n"
    "  background: url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
    "viewBox='0 0 @W@ @H@' preserveAspectRatio='none'%3E%3Cdefs%3E"
    "%3ClinearGradient id='g' gradientUnits='userSpaceOnUse' x1='{{ mx }}' y1='{{ my }}' "
    "x2='{{ ex | round(1) }}' y2='{{ ey | round(1) }}'%3E"
    "%3Cstop offset='0' stop-color='rgb(@RGB@)' stop-opacity='{{ a }}'/%3E"
    "%3Cstop offset='@FULL@' stop-color='rgb(@RGB@)' stop-opacity='{{ a }}'/%3E"
    "%3Cstop offset='1' stop-color='rgb(@RGB@)' stop-opacity='0'/%3E%3C/linearGradient%3E"
    "%3Cfilter id='b' filterUnits='userSpaceOnUse' x='0' y='0' width='@W@' height='@H@'%3E"
    "%3CfeGaussianBlur stdDeviation='@BLUR@'/%3E%3C/filter%3E%3C/defs%3E"
    "%3Cpolygon points='{{ pts }}' fill='url(%23g)' filter='url(%23b)'/%3E%3C/svg%3E\") "
    "center / 100% 100% no-repeat;\n}"
)

_WASH = (
    "{% set a = (@OUTDOOR@ * clear * up * (0.4 + 0.6 * ([sin(el * r), 0] | max))) | round(2) %}\n"
    ":host {\n  transition: opacity 0.4s ease;\n"
    "  opacity: {{ 1 if el > 0 else 0 }};\n"
    "  background: rgba(@RGB@, {{ a }});\n}"
)


_MIN_HALF = 30   # plan px: half the narrowest beam at the window


def _fill(template: str, values: dict) -> str:
    for k, v in values.items():
        template = template.replace(f"@{k}@", str(v))
    return template


def _sun_values(model: dict) -> dict:
    sun = M.settings(model, "sun")
    w, h = model["size"]
    return {"SUN": sun["sun_entity"], "WEATHER": sun["weather_entity"], "NORTH": sun["north_deg"],
            "CLOUD_DIM": sun["cloud_dim"], "PPM": sun["px_per_m"], "HEIGHT": sun["height_m"],
            "MAXPX": sun["max_px"], "SPREAD": sun["spread"], "STRENGTH": sun["strength"],
            "OUTDOOR": sun["outdoor_strength"], "BLUR": sun["blur"], "W": w, "H": h,
            "RGB": ", ".join(str(int(v)) for v in sun["color"])}


def beam_template(model: dict, frame: dict) -> str:
    nx, ny = frame["normal_out"]
    base = _sun_values(model)
    ov = frame.get("sun") or {}
    for key, name in (("height_m", "HEIGHT"), ("spread", "SPREAD"), ("blur", "BLUR"), ("strength", "STRENGTH")):
        if key in ov:
            base[name] = ov[key]
    base["FULL"] = ov.get("full_until", 0)
    if "fade_from_deg" in ov:
        lo, hi = ov["fade_from_deg"], ov["fade_to_deg"]
        base["FADE"] = f"{{% set fade = [[({hi} - el) / ({hi} - {lo}), 1] | min, 0] | max %}}"
    else:
        base["FADE"] = "{% set fade = 1 %}"
    # A narrow window still lights a patch you can see: give it a minimum beam width, and
    # blur no more than about 40 % of that, so the blur doesn't wash a thin beam out.
    half = max(frame["half"], _MIN_HALF)
    if "blur" not in ov:
        base["BLUR"] = round(min(float(base["BLUR"]), half * 0.4), 1)
    v = {**base, "MX": round(frame["mx"], 1), "MY": round(frame["my"], 1),
         "TX": frame["tx"], "TY": frame["ty"], "HALF": round(half, 1), "NX": nx, "NY": ny}
    return _fill(_SUN_COMMON + _BEAM, v)


def wash_template(model: dict) -> str:
    return _fill(_SUN_COMMON + _WASH, _sun_values(model))


def sun_names(model: dict) -> list[str]:
    return ([f"window:{o}" for o in M.sun_windows(model)]
            + [f"outdoor:{a}" for a in sorted(model.get("outdoors") or {})])


def _sun_item(model: dict, name: str):
    kind, _, ident = name.partition(":")
    if kind == "window":
        return M.sun_windows(model)[ident]
    return model["outdoors"][ident]


def sun_key_for(model: dict, name: str) -> str:
    return f"sun:{name}:{M.sun_key(model, name, _sun_item(model, name))}"


def stale_sun(model: dict, reuse: dict[str, str]) -> list[str]:
    return [n for n in sun_names(model) if sun_key_for(model, n) not in reuse]


def sun_elements(model: dict, geo: "M.Geometry | None", reuse: dict[str, str]) -> tuple[list[dict], dict]:
    """Sunlight layers (outdoor washes, then window beams); masks are reused when unchanged."""
    todo = stale_sun(model, reuse)
    if todo and geo is None:
        raise RuntimeError("geometry needed to rebuild sun masks")
    scale = M.settings(model, "light")["mask_scale"]
    fresh = M.sun_masks(geo, set(todo)) if todo else {}
    sun_entity = M.settings(model, "sun")["sun_entity"]
    washes, beams = [], []
    stats = {"regenerated": [], "reused": []}
    for name in sun_names(model):
        key = sun_key_for(model, name)
        if name in fresh:
            uri = engine.mask_data_uri(fresh[name], scale)
            stats["regenerated"].append(name)
        else:
            uri = reuse[key]
            stats["reused"].append(name)
        if name.startswith("outdoor:"):
            washes.append(glow_element(sun_entity, key, uri, wash_template(model)))
        else:
            beams.append(glow_element(sun_entity, key, uri, beam_template(model, _sun_item(model, name))))
    return washes + beams, stats


def glow_element(entity: str, key: str, uri: str, template: str) -> dict:
    return {
        "type": "image", "entity": entity, "image": _BLANK,
        "tap_action": {"action": "none"}, "hold_action": {"action": "none"},
        "style": {
            "left": "0", "top": "0", "width": "100%", "height": "100%", "transform": "none",
            "pointer-events": "none",
            "--fp-key": key,
            "--fp-mask": f"url('{uri}')",
            "mask-image": "var(--fp-mask)", "-webkit-mask-image": "var(--fp-mask)",
            "mask-size": "100% 100%", "-webkit-mask-size": "100% 100%",
        },
        "card_mod": {"style": template},
    }


_ANCHOR = {  # (x part, y part) of the translate; P = padding
    "center": ("-50%", "-50%"), "n": ("-50%", "calc(-100% - P)"), "s": ("-50%", "P"),
    "e": ("P", "-50%"), "w": ("calc(-100% - P)", "-50%"),
    "ne": ("P", "calc(-100% - P)"), "nw": ("calc(-100% - P)", "calc(-100% - P)"),
    "se": ("P", "P"), "sw": ("calc(-100% - P)", "P"),
}


def icon_transform(anchor: str, pad: float, offset: tuple[float, float], scale: float) -> str:
    """Where the icon sits relative to its point: e.g. "se" = below-right, `pad` px clear."""
    parts = []
    for part, off in zip(_ANCHOR[anchor], offset):
        part = part.replace("P", f"{pad:g}px")
        if off:
            part = f"calc({part.removeprefix('calc(').removesuffix(')')} + {off:g}px)"
        parts.append(part)
    t = f"translate({parts[0]}, {parts[1]})"
    return t + (f" scale({scale:g})" if scale != 1 else "")


def _halo(rgb: list[int]) -> str:
    default = ", ".join(str(int(v)) for v in rgb)
    return (
        "{% set e = config.entity %}{% set on = is_state(e, 'on') %}"
        f"{{% set rgb = state_attr(e, 'rgb_color') or [{default}] %}}"
        "{% set b = (state_attr(e, 'brightness') or 255) | int / 255 %}\n"
        ":host {\n  transition: box-shadow 0.4s ease;\n"
        "  box-shadow: 0 0 0 1px var(--divider-color){% if on %}, 0 0 {{ (6 + 10 * b) | round }}px "
        "{{ (1 + 3 * b) | round }}px rgba({{ rgb | join(', ') }}, {{ (0.35 + 0.5 * b) | round(2) }}){% endif %};\n}"
    )


# `rotate` composes with the positioning `transform`, so the icon spins in place.
_SPIN = (
    "{% set on = is_state(config.entity, 'on') %}\n"
    ":host {\n  animation: {{ 'fp-spin 1.2s linear infinite' if on else 'none' }};\n}\n"
    "@keyframes fp-spin { to { rotate: 360deg; } }"
)

_DEFAULT_ICONS = {"light": "mdi:ceiling-light", "fan": "mdi:fan", "device": ""}


def icon_element(model: dict, fx: dict) -> dict:
    w, h = model["size"]
    x = fx.get("icon_x", fx["x"])
    y = fx.get("icon_y", fx["y"])
    el = {
        "type": "state-icon", "entity": fx["entity"],
        "tap_action": {"action": "toggle"}, "hold_action": {"action": "more-info"},
        "style": {
            "left": _pct(x, w), "top": _pct(y, h), "border-radius": "50%",
            "background-color": "var(--card-background-color)",
            "transform": icon_transform(fx.get("icon_anchor", "center"), fx.get("icon_pad", 6),
                                        tuple(fx.get("icon_offset", [0, 0])), fx.get("icon_scale", 1.25)),
        },
    }
    icon = fx.get("icon") or _DEFAULT_ICONS[fx["kind"]]
    if icon:
        el["icon"] = icon
    if fx["kind"] == "light":
        el["card_mod"] = {"style": _halo(M.glow(model, fx)["default_rgb"])}
    elif fx["kind"] == "fan":
        el["card_mod"] = {"style": _SPIN}
    return el


def previous_masks(card: dict | None) -> dict[str, str]:
    """{mask key: data URI} from a card this module built before, to reuse unchanged masks."""
    out = {}
    for el in (card or {}).get("elements", []):
        st = el.get("style") or {}
        if "--fp-key" in st and "--fp-mask" in st:
            out[st["--fp-key"]] = st["--fp-mask"][len("url('"):-len("')")]
    return out


def build_elements(model: dict, geo: "M.Geometry | None", reuse: dict[str, str],
                   only_rebuild: set[str] | None = None, workers: int = 1) -> tuple[list[dict], dict]:
    """All card elements (glows first, then icons) and build stats.

    A fixture's masks are recomputed only when no mask with its current key
    exists in `reuse` (or it is in `only_rebuild`); `geo` may be None when
    nothing needs recomputing. Recomputation is spread over `workers` processes.
    """
    w, h = model["size"]
    glows, icons = [], []
    todo = sorted(set(stale_fixtures(model, reuse)) | {
        f for f in (only_rebuild or set()) if model["fixtures"].get(f, {}).get("kind") == "light"})
    if todo and geo is None:
        raise RuntimeError("geometry needed to rebuild masks")
    computed = M.compute_parallel(geo, todo, workers) if todo else {}
    stats = {"regenerated": [], "reused": [], "warnings": []}
    for fid, fx in sorted((model.get("fixtures") or {}).items()):
        if fx["kind"] == "light":
            key = M.fixture_key(model, fid)
            if fid in computed:
                variants, warnings = computed[fid]
                stats["warnings"] += warnings
                stats["regenerated"].append(fid)
            else:
                cached = {k: v for k, v in reuse.items() if k.split(":")[0] == key}
                variants = [(_decode_assign(k), uri) for k, uri in sorted(cached.items())]
                stats["reused"].append(fid)
            g = M.glow(model, fx)
            for assign, uri in variants:
                tpl = glow_template(g, _pct(fx["x"], w), _pct(fx["y"], h), w / h, assign)
                glows.append(glow_element(fx["entity"], f"{key}:{_encode_assign(assign)}", uri, tpl))
        icons.append(icon_element(model, fx))
    suns, sun_stats = sun_elements(model, geo, reuse)
    stats["sun_regenerated"], stats["sun_reused"] = sun_stats["regenerated"], sun_stats["reused"]
    return suns + glows + icons, stats


def _encode_assign(assign: dict[str, bool]) -> str:
    return ",".join(f"{s}={int(v)}" for s, v in sorted(assign.items()))


def _decode_assign(key: str) -> dict[str, bool]:
    tail = key.split(":", 1)[1] if ":" in key else ""
    return {s: v == "1" for s, v in (p.split("=") for p in tail.split(",") if p)}


def stale_fixtures(model: dict, reuse: dict[str, str]) -> list[str]:
    keys = {k.split(":")[0] for k in reuse}
    return sorted(fid for fid, fx in (model.get("fixtures") or {}).items()
                  if fx["kind"] == "light" and M.fixture_key(model, fid) not in keys)


def build_card(model: dict, elements: list[dict]) -> dict:
    card = {"type": "picture-elements", "image": model["image"], "elements": elements,
            "grid_options": {"columns": "full"}, M.CARD_MARKER: True}
    if model.get("dark_image"):
        card["dark_mode_image"] = model["dark_image"]
    return card


def find_card(config: dict) -> tuple[list, int] | None:
    """(the list holding the floor-plan card, its index), searching every view and section."""
    def walk(cards):
        for i, c in enumerate(cards):
            if isinstance(c, dict) and c.get(M.CARD_MARKER):
                return cards, i
            for key in ("cards",):
                if isinstance(c, dict) and isinstance(c.get(key), list):
                    found = walk(c[key])
                    if found:
                        return found
        return None

    for view in config.get("views", []):
        for holder in [view] + list(view.get("sections") or []):
            found = walk(holder.get("cards") or [])
            if found:
                return found
    return None


def adopt_card(config: dict) -> bool:
    """Mark the first picture-elements card as the floor-plan card (to take over a hand-built one)."""
    for view in config.get("views", []):
        for holder in [view] + list(view.get("sections") or []):
            for card in holder.get("cards") or []:
                if isinstance(card, dict) and card.get("type") == "picture-elements":
                    card[M.CARD_MARKER] = True
                    return True
    return False


def place_card(config: dict, card: dict, title: str) -> None:
    """Replace the floor-plan card in place, or add a full-width sections view holding it."""
    found = find_card(config)
    if found:
        holder, i = found
        holder[i] = card
        return
    config.setdefault("views", []).append({
        "title": title or "Floor Plan", "path": "floor-plan", "icon": "mdi:floor-plan",
        "type": "sections", "max_columns": 2,
        "sections": [{"type": "grid", "column_span": 2, "cards": [card]}],
    })


# --- Preview -----------------------------------------------------------------

_OPENING_COLOURS = {"open": (46, 160, 67), "closed": (215, 58, 73), "sensor": (31, 111, 235)}
_KIND_COLOURS = {"window": (0, 170, 200), "glass_door": (150, 80, 200), "open_air": (60, 180, 90)}


def preview(model: dict, geo: "M.Geometry", fixture_id: str = "", brightness: float = 1.0,
            show_gaps: bool = False, max_width: int = 1100) -> bytes:
    """PNG of the plan with walls, openings, blockers, fixtures and a coordinate grid.

    With `fixture_id`, that fixture's glow is drawn as the dashboard would show it
    at `brightness` (mask x radial falloff), with doors in their closed state.
    """
    w, h = model["size"]
    base = np.stack([geo.gray] * 3, -1).astype(np.float32)
    base = base * 0.55 + 255 * 0.45                       # fade the drawing
    base[geo.raw] = (40, 40, 40)
    base[geo.sealed & ~geo.raw] = (245, 160, 60)           # gaps the sealing closed
    if fixture_id:
        fx = model["fixtures"][fixture_id]
        g = M.glow(model, fx)
        mask = geo.lit(fixture_id, {}).astype(np.float32) / 255
        shape = M.glow_shape(g, w, brightness)
        yy, xx = np.mgrid[0:h, 0:w]
        d = np.hypot(xx - fx["x"], yy - fx["y"]) / max(shape["radius_px"], 1)
        prof = np.interp(d, [0, 0.45, 1], [1, 0.45, 0], right=0) * shape["alpha"] * mask
        base = base * (1 - prof[..., None]) + np.array(shape["rgb"], np.float32) * prof[..., None]
    img = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img, "RGBA")
    for x in range(0, w, 100):
        d.line([(x, 0), (x, h)], fill=(0, 0, 0, 28))
        d.text((x + 2, 2), str(x), fill=(0, 0, 0, 150))
    for y in range(0, h, 100):
        d.line([(0, y), (w, y)], fill=(0, 0, 0, 28))
        d.text((2, y + 2), str(y), fill=(0, 0, 0, 150))
    for bid, bl in (model.get("blockers") or {}).items():
        _draw_shape(d, bl, (200, 0, 160, 90), geo.line_width)
        _label(d, bl, bid, (150, 0, 120))
    for oid, op in (model.get("openings") or {}).items():
        colour = _KIND_COLOURS.get(op["kind"], _OPENING_COLOURS[op.get("state", "closed")])
        _draw_shape(d, op, (*colour, 170), geo.line_width)
        _label(d, op, f"{oid} {op['facing']}" if op.get("facing") else oid, colour)
    for aid, area in (model.get("outdoors") or {}).items():
        x, y = area["x"], area["y"]
        d.polygon([(x, y - 7), (x + 7, y), (x, y + 7), (x - 7, y)], fill=(60, 180, 90, 255), outline=(0, 0, 0, 255))
        d.text((x + 9, y - 6), f"{aid} (outdoors)", fill=(0, 90, 30, 255))
    if show_gaps:
        for gap in geo.gaps():
            x0, y0, x1, y1 = gap["rect"]
            d.rectangle([x0, y0, x1, y1], outline=(245, 120, 0, 255))
            d.text((x1 + 3, y0), f"{gap['id']} {gap['guess']}", fill=(180, 80, 0, 255))
    for fid, fx in sorted((model.get("fixtures") or {}).items()):
        x, y = fx["x"], fx["y"]
        colour = (230, 160, 0) if fx["kind"] == "light" else (60, 120, 220)
        d.ellipse([x - 6, y - 6, x + 6, y + 6], fill=(*colour, 255), outline=(0, 0, 0, 255))
        d.text((x + 8, y - 6), fid, fill=(0, 0, 0, 255))
    if w > max_width:
        img = img.resize((max_width, round(h * max_width / w)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _draw_shape(d, item, fill, width):
    if "rect" in item:
        x0, y0, x1, y1 = item["rect"]
        d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=fill)
    else:
        d.line(item["line"], fill=fill, width=width)


def _label(d, item, text, colour):
    x, y = (item.get("rect") or item.get("line"))[:2]
    d.text((x + 2, y - 12), text, fill=(*colour, 255))
