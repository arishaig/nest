---
name: ha-floorplan
description: Build and calibrate Home Assistant floor-plan dashboards through nest-mcp, with icons, a glow per light that follows walls, doors and windows, and real sunlight through the windows. Use when asked to add, move or remove lights, fans or devices on the floor plan, change doors, windows or walls, add sunlight or a sunny balcony, start a floor plan from a new plan image, or calibrate how the glows match the real rooms.
---

# Floor-plan dashboards

The `floorplan_*` tools in nest-mcp keep the whole model (fixtures, openings,
blockers, glow calibration, undo history) inside the dashboard's own config
under `nest_floorplan`. Each change regenerates the floor-plan card and saves it
live. The wall shadow masks are embedded in the card as small PNG data URIs,
so nothing has to be uploaded to `/config/www`. The plan image must already be
served by HA (e.g. `/local/floorplan/plan.svg`). The repo is public: never write
a model or dashboard config into the working tree.

The glows need the **card-mod** frontend plugin. Icons work without it.

## Tools

| Tool | Use |
|---|---|
| `floorplan_get(url_path, include_gaps=False, raw=False, include_thin_walls=False)` | Read-only: the model; `include_gaps` lists detected wall gaps (candidate doors/windows, with a `suggested_facing` for outside windows); `include_thin_walls` lists hairline walls the detector skipped; `raw` returns the stored model to back up |
| `floorplan_audit(url_path)` | Read-only checklist: windows without a facing, missing entities, weather without cloud_coverage, unblocked wall-to-wall hairlines. Run it before saying a plan is done |
| `floorplan_import(url_path, model, adopt_card=False)` | Load a whole model (migration/restore); `adopt_card` takes over a hand-built picture-elements card in place |
| `floorplan_preview(url_path, fixture_id="", brightness_pct=100, show_gaps=False, show_thin_walls=False, sun_azimuth=None, sun_elevation=None, cloud_pct=0, open_sensors=None)` | Read-only image: walls, openings, blockers, fixtures, 100 px grid; with `fixture_id`, that glow as the dashboard draws it; with `sun_azimuth` + `sun_elevation`, the sunlight for that sun (amber) plus a per-window report of the gates that scaled it |
| `floorplan_create(url_path, image, ...)` | Start a floor plan on a dashboard (or a new one with `create_dashboard=True`) |
| `floorplan_set_fixture(url_path, fixture_id, entity, x, y, ...)` | Add or move a light/fan/device; icon placement options |
| `floorplan_remove_fixture` | Remove one |
| `floorplan_set_opening(url_path, opening_id, from_gap / rect / line, kind, state, sensor, transmission, facing, sun)` | Doors, windows, glass doors, archways, `open_air` edges; `state="sensor"` follows a door contact; `facing` turns a window into a sun source |
| `floorplan_set_outdoor(url_path, area_id, x, y)` | Mark an enclosed area (a balcony) as open air: faint sky-light wash whenever the sun is up |
| `floorplan_set_blocker(url_path, blocker_id, rect / line / from_thin_wall, fixtures)` | Undrawn walls, or furniture that shades some lights (e.g. an island); `from_thin_wall` adopts a hairline found by `include_thin_walls` |
| `floorplan_calibrate(url_path, fixture_id / entity, size, strength, ...)` | Glow tuning; quick (no mask regeneration) |
| `floorplan_settings` | Model-wide wall detection, soft-shadow, transmission and sun settings |
| `floorplan_undo` / `floorplan_rebuild` | Revert the last change (10 kept) / regenerate the card |
| `floorplan_calibration(url_path, action, ...)` | Drive the real lights: `start` (snapshot + pause automations), `show` (one light alone), `end` (restore) |

Coordinates are plan pixels (the SVG viewBox). Read them off `floorplan_preview`'s grid.

## Setting up a plan

1. `floorplan_create`, then check the preview: dark = walls the detector found,
   orange = gaps it sealed. Furniture outlines must not show up as walls; if they
   do, tune `floorplan_settings(walls={...})`.
2. Make each doorway/window an opening (`from_gap` with the listed gap id). A
   closed door blocks light, a window passes 70 %, a glass door 35 %. Doors with
   a contact sensor get `state="sensor"`, which gives two glow layers per
   affected light. Diagonal doors, or gaps wider than `seal_gap`, take a `line`.
3. Add fixtures at the real light positions. Several fixtures can share one
   entity (two ceiling lights on one switch). Lamps against a wall usually want
   the icon on the wall: `icon_x`/`icon_y` on the wall face, `icon_anchor`
   pointing into the room, default `icon_pad` 6.
4. Preview each light (`fixture_id`) before showing the user.

## Sunlight

Every `window`, `glass_door` or `open_air` opening with a `facing` throws a sun beam
into its room, drawn live by card-mod from `sun.sun` (azimuth, elevation) and
`weather.*` `cloud_coverage`; nothing is stored per time of day.

1. `facing` is the way the window *looks out* in plan terms (`n` = plan up). Plan up is
   compass north by default; if not, set `floorplan_settings(sun={"north_deg": ...})`.
   A window on a vertical wall can only face `e`/`w`, on a horizontal wall `n`/`s`, a diagonal door `ne`/`nw`/`se`/`sw`.
   A window between a room and a balcony faces the balcony.
2. The beam starts at the window, fans out (`spread`), fades along its length and is
   clipped to what the window can see, so walls and closed doors stop it. Its length
   is `height_m * px_per_m / tan(elevation)`: set `px_per_m` from one real measurement
   (interior doors are about 80-90 px). A window narrower than 60 px still throws a
   60 px beam.
3. Under cloud the beam dims by `cloud_dim` of its strength; below the horizon, or when
   the sun is on the wrong side of the window, it is off.
4. A covered balcony open on one side: add an `open_air` opening along that edge
   (`facing` = the open side, `sun={"height_m": <ceiling height>, "spread": 1,
   "full_until": 0.85}`): a high sun lights only a strip, a low sun reaches deeper. Add
   `floorplan_set_outdoor` for the faint even sky-light under the beam.
5. A window *behind* a covered balcony only sees sun low enough to pass under the
   balcony ceiling: give it `sun={"fade_from_deg": 15, "fade_to_deg": 40}` and its beam
   fades out as the sun climbs (about 34 deg blocks the sill ray at a 2.4 m ceiling and
   2.2 m of balcony depth; widen the range for a deeper balcony).
   For a window that gets its sun *through the balcony's open edge*, also add
   `"through": "<the open_air opening's id>"`: the window is then lit only while the ray
   from it to the sun actually crosses that edge, so it goes dark when the balcony's own
   walls would shadow it.
   A glass door (`kind="glass_door"`) lets sun in too: its beam is attenuated by the same
   closed-door transmission the light spilling the other way uses (the opening's own
   `transmission`, else the glass_door setting, 0.35), and is full strength when the door
   is open (`state="open"`, or its `sensor` reads on). A diagonal door takes a diagonal
   `facing` (`ne`/`nw`/`se`/`sw`): pick the one that points out of the door at right
   angles to its line. A glass door behind a balcony wants the same `through` and a
   `fade` (it reaches the floor, so about 25 to 50 degrees).
6. **Thin walls.** The wall detector misses very thin double-line walls, so light leaks
   through them. Look at `floorplan_preview` (dark = walls found) and add a
   `floorplan_set_blocker` along any wall that is missing.
7. Sun masks are rebuilt when walls, openings, blockers or facings change; `sun`
   overrides and sun settings only change the template (instant). Doors that follow a
   sensor are treated as closed for sunlight.

Beams are warm white, so they are faint on a pale plan in light mode and obvious in dark
mode: judge visibility in the theme the user actually uses.

## Start to finish (a whole plan, with sun)

1. `floorplan_create` (new dashboard, `create_dashboard=True`) from the served plan image.
   Never build over an existing dashboard; make a new one and compare side by side.
2. `floorplan_get(include_gaps=True, include_thin_walls=True)` and `floorplan_preview(show_gaps=True,
   show_thin_walls=True)`. Adopt gaps with `floorplan_set_opening(from_gap=...)`: kind, state
   (`open` for doors always left open, `sensor` + a contact for doors that move), `facing` for
   windows (use the suggestion; set balcony-side ones by hand). Gaps on the plan's border (a window
   at x=0) are not detected: give them a `rect`.
3. Thin walls: a partition drawn as a pair of hairlines is invisible to the wall detector and light
   leaks through it. Adopt the wall-to-wall double lines (`from_thin_wall`); ignore furniture edges.
4. A covered balcony: an `open_air` opening on the open side + `floorplan_set_outdoor`; windows and
   glass doors behind it get `sun={"through": <the open edge>, "fade_from_deg": ..., "fade_to_deg": ...}`.
5. `floorplan_set_fixture` for every light, fan and device (fans spin; use `kind="device"` for a
   plain icon with no glow), then `floorplan_calibrate` per light. Take positions and icon anchors
   from the old dashboard if there is one.
6. **Verify the sun without waiting for it**: `floorplan_preview(sun_azimuth, sun_elevation, cloud_pct)`
   at a few angles (low west, noon, high summer). The report says which gate darkened each window.
7. `floorplan_audit`, fix what it names, and only then ask the user to look.
8. Ask for a **fresh page load** (Ctrl+Shift+R) every time: a cached older dashboard has cost whole
   debugging sessions. Delete scratch dashboards with `ha_delete_dashboard` when done (confirm first).

## Gotchas

- **Never paste a mask data URI into `ha_save_dashboard` by hand.** One wrong character in the base64
  makes the browser reject the PNG, and a layer whose mask fails to load disappears. Let these tools
  write masks. If you must inspect one, CRC-check its PNG chunks.
- **After a nest-mcp deploy** there are two rollouts (the image, then Flux's pod spec). Wait until
  `k8s_pods nest-mcp` shows three new, ready pods before using the tools; the connector may need
  `/mcp` to refresh its tool list.
- **Calls that edit one dashboard must run one at a time**: each loads, changes and saves the whole
  config, so parallel calls can lose an update.
- **Beam colour is warm white**: nearly invisible on a pale plan in light mode, obvious in dark mode.
  A beam that "isn't there" may only be faint: check with the preview simulation first.
- **Fans** are positioned with the CSS `translate`/`scale` properties, not `transform`: `rotate` runs
  before `transform`, so a transform offset would make the icon orbit its anchor while spinning.
- The dashboard config can reach several hundred KiB with all masks (it still saves); raise
  `light.mask_scale` if loading is slow.

## Calibration session

Do it after dark, one light at a time, with the user looking at the room and
the dashboard.

1. Agree with the user which automations to pause (motion lights, circadian
   colour, night lights: anything that changes lights), then
   `floorplan_calibration(action="start", pause_automations=[...])`.
2. Per light: `action="show"` at 100 %, then 40 %, then 10 % warm white. For
   colour lights, follow with cool white and a saturated colour.
3. **Every suggested change must be applied live and confirmed by the user
   before moving on.** Never batch changes or move on without a "looks good".
4. Ask for and give feedback in plain words ("reaches a little less far",
   "a little brighter"), never as numbers on a scale. Before changing anything,
   say how you read the feedback ("the real light reaches further, so the glow
   grows"). Direction is easy to invert: "it's further in real life" means the
   glow is too small.
5. Map the words to `floorplan_calibrate`:
   - reach: `size` (±10 % for "a little", ±25 % for "quite a bit")
   - brightness: `strength` (±8-15 %)
   - right at 100 % but off at 40 %: `reach_curve` / `intensity_curve`
   - right at 100 and 40 % but off at 10 %: `dim_reach` / `dim_boost`. These
     only act below 40 %, so they leave the confirmed levels alone.
   - "whiter in real life": `whiten`. For plain on/off lights (no colour
     attributes), "yellower"/"whiter" means `default_rgb`.
   - If the light is in the wrong place, use `floorplan_set_fixture`, not
     calibration.
6. Always `action="end"`. It restores every light and resumes the automations.
   Check that the paused automations really are back on.
