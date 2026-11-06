---
name: ha-dashboards
description: Read, snapshot, build, and replace Home Assistant dashboards (Lovelace) through nest-mcp. Use when asked to review, redesign, clean up, or edit HA dashboards, or to check which custom cards a dashboard still depends on.
---

# Home Assistant dashboards

Dashboard configs live only in HA's `.storage` on VM 107. They are **not**
stored in this repo: the repo is public and the configs are personal. Never
write a dashboard config into the working tree. Use the scratchpad.

Tools (nest-mcp, all over HA's WebSocket API):

| Tool | Use |
|---|---|
| `ha_list_dashboards()` | Read-only. Lists every dashboard, including the default (`url_path` `""`), each with its mode (`storage` or `yaml`), plus the frontend `resources` (custom-card JS/CSS) that are still loaded |
| `ha_get_dashboard(url_path="")` | Read-only. Returns the stored config, in the shape `ha_save_dashboard` accepts. `generated: true` / `config: null` means an auto-generated (strategy) dashboard with nothing stored |
| `ha_save_dashboard(url_path, config, create=False, title="", icon="", show_in_sidebar=True)` | Destructive. Replaces a storage dashboard's whole config, or creates a new one with `create=True`. A new dashboard needs a `title`, and its `url_path` must contain a hyphen. Returns the config it replaced as `previous` |

There are no delete or resource-removal tools. Deleting dashboards and
uninstalling custom cards (Settings > Marketplace) happen in the UI, after the
user confirms.

## Workflow

1. **Snapshot first.** `ha_list_dashboards`, then `ha_get_dashboard` for each
   storage dashboard. Save the JSON to the scratchpad before any edits.
2. **Inventory.** Collect card types (`custom:*` means a custom card) and
   every `entity` / `entities` reference. Check the references against
   `ha_list_entities`, and compare the `custom:*` types with the `resources`
   list.
3. **Build side by side.** Create the new dashboard under a new `url_path`
   (e.g. `home-v2`) with `create=True`. Never overwrite a dashboard the user
   still relies on until they have reviewed the replacement.
4. **Every save: write `previous` to the scratchpad straight away.** The tool
   result disappears when the conversation is summarized, and `previous` is
   the only rollback copy. To revert, save `previous` back.
5. **Verify.** Re-read with `ha_get_dashboard`. Check that it has no
   `custom:` card types (unless intended) and that every referenced entity
   exists. Then the user checks it on phone and desktop.

## Notes

- YAML-mode dashboards are read-only here. Saving one fails with a clear
  error.
- From HA 2026.11, prefer built-in building blocks: the sections view, tile and
  heading cards, automatic sections (Device / Entities), and visibility
  conditions. Automatic sections keep themselves current as devices change.
- From 2026.11, entity display names default to the device name plus the
  entity name. Set a card `name`, or mark the entity as the device's main
  entity, if a label reads twice.
