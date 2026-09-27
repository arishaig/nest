---
name: ha-automations
description: Audit, export, and fix Home Assistant automations through nest-mcp. Use when an automation didn't fire, lights/devices didn't react, after re-pairing a Zigbee device, when asked to review or clean up HA automations, or to snapshot automations before a bulk edit.
---

# Home Assistant automations

HA automations live only in HA (VM 107). They are **not** stored in this repo:
the repo is public and the configs are personal. Never write exports into the
working tree; use the scratchpad if a file is needed.

Tools (nest-mcp):

| Tool | Use |
|---|---|
| `ha_audit_automations(automation_id="", include_info=True)` | Read-only. Lints for known bad patterns and reconciles every reference against live HA |
| `ha_export_automations(automation_id="")` | Read-only. Full configs, in the shape `ha_save_automation` accepts |
| `ha_get_automation` / `ha_save_automation` / `ha_delete_automation` | Read one, replace one (destructive), delete one (destructive) |

## Workflow

1. **Audit.** Run `ha_audit_automations`. For "X didn't fire", audit that
   automation by id (from `ha_list_automations`), then check its trigger
   entity's state with `ha_get_state`. `unavailable` since a timestamp is
   usually the answer.
2. **Triage** by severity. `error` = it can't work as written. `warning` = it
   works today but fails silently under a known condition. `info` (disabled,
   never fired) is context, not a to-do. "Never fired" on a rare alert is fine.
3. **Snapshot before bulk edits**: `ha_export_automations`, saved to the
   scratchpad, so an edit can be reverted by saving the old config back.
   Check `skipped` is empty first; anything listed there isn't in the snapshot.
4. **Fix one at a time.** `ha_get_automation`, edit the whole config, show the
   user the before/after trigger (or changed block), get a yes, then
   `ha_save_automation` with the id. Add a short note to the automation's
   `description` saying why it changed, as the existing ones do.
5. **Verify** by re-running `ha_audit_automations(automation_id=...)`.

## Fix recipes

- **`device_trigger`** → state trigger on the entity. The finding's
  `suggested_entities` lists the device's entities in the trigger's domain; pick
  the one matching the trigger `type`, and ask if it's ambiguous. Map the type
  to `to:`: `opened`/`occupied`/`turned_on`/`vibration` → `'on'`;
  `not_opened`/`not_occupied`/`turned_off`/`no_vibration` → `'off'`. Keep `for:`.
  Don't add `from:` (see the next rule).
- **`from_state_misses_recovery`** → drop `from:` from the trigger, keeping `to:`.
  The trigger then fires when a door sensor drops off Zigbee and comes back already open.
- **`unavailable_entity`** → a device problem, not a config one. Report which
  device and since when. Zigbee: wake it or re-pair it in Zigbee2MQTT. If
  several dropped at the same second, suspect a shared router. After a
  re-pair, re-audit: entity ids usually survive, device ids don't.
- **`missing_entity` / `missing_service` / `missing_area` / `missing_device`** → find
  the renamed replacement (`ha_list_entities`, `ha_list_areas`) and confirm it
  with the user; don't guess.
- **`config_invalid`** → `ha_get_automation`; if HA rejects a save, its
  validation message says why.

## Adding a new pattern

When an automation fails silently in a new way, add a rule to
`_lint_automation` (or a reference check to `_audit`) in
`mcp/nest_mcp/tools/homeassistant.py`. Add a comment naming the incident, and a
test in `mcp/tests/test_homeassistant.py`. Keep rules narrow enough that a
clean audit stays quiet: a noisy rule gets ignored.
