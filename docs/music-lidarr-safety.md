# Lidarr safety net (music stack, phase 0)

Read this before running `tools/lidarr-triage` in apply mode, or doing any
other bulk change to Lidarr. It covers what protects Lidarr's database, how to
restore it, the manual backup to take right before a cleanup, and — the
important part — what does **not** protect the music files themselves.

Lidarr endpoints below were checked against the OpenAPI spec for Lidarr
v3.1.2 (the running version is 3.1.2.4913, `prarr:lidarr-plugins` image).

## What is protected, what isn't

| Data | Where | Protection |
|---|---|---|
| Lidarr DB + config (`lidarr.db`, `config.xml`, `Backups/`) | PVC `lidarr-config` (`nfs-nvme`) → PVE `/rpool/data/k8s-configs/media/lidarr-config` | Daily 03:00 PBS host backup `host/k8s-configs` into `pbs-local` (`playbooks/provision/pbs.yml`), plus Lidarr's own scheduled backups in `/config/Backups/scheduled` (which PBS also captures) |
| Music files | `media-nfs` PV → `/Tank/media_root/media/music` (`/data/media/music` in the pod) | **None.** `Tank/media_root` is deliberately unbacked re-acquirable data (finding B4) and has no ZFS snapshots. A deleted track file is gone unless Lidarr's Recycling Bin caught it |

The PBS pxar job copies `lidarr.db` while Lidarr is running, so a PBS
snapshot is only as consistent as SQLite's on-disk state at that moment. A
Lidarr **native backup** (below) is taken by Lidarr itself and is the reliable
restore point; PBS is the fallback (and the thing that survives losing the PVC).

Check the latest PBS run: nest-mcp `proxmox_backup_status` → look for
`pbs-local:host/k8s-configs` with status `OK`.

## Precondition: Lidarr Recycling Bin

`DELETE /api/v1/trackfile/{id}` and `DELETE /api/v1/trackfile/bulk` go through
Lidarr's `RecycleBinProvider`: if **Recycling Bin** is empty, files are
**deleted permanently**; if set, they are moved there (under an
artist-relative subfolder). `tools/lidarr-triage apply` refuses to run while it
is unset.

Set it once, in the Lidarr UI (Settings → Media Management → Show Advanced →
Importing/File Management):

- **Recycling Bin:** `/data/lidarr-recycle` — i.e. `/Tank/media_root/lidarr-recycle`.
  Same filesystem as the library, so the move is a cheap rename. It sits
  outside `/data/media/`, so it must not be inside any Jellyfin library: check
  Jellyfin → Dashboard → Libraries → each library's folders, and confirm none
  is `/data` itself or a parent of `/data/lidarr-recycle`. Otherwise deleted
  tracks reappear in Jellyfin (Jellyfin mounts `/data` read-write) and get
  analysed by AudioMuse.
- **Recycling Bin Cleanup:** e.g. 30 days — long enough to notice a mistake.

This is a UI change stored in Lidarr's DB (it isn't in git), so a DB restore
from before today's date also reverts it — re-check it after any restore.

## Manual native backup (right before every cleanup run)

Use the LAN LoadBalancer, not `lidarr.arishaig.site`: that hostname is behind
Authelia `two_factor` with no API bypass, so API-key calls get the login page.

```bash
# The key never goes on the command line history in plaintext if you read it
# like this (prompts for the vault password):
export LIDARR_API_KEY="$(ansible-vault view inventory/group_vars/all/vault.yml \
  | awk '/^lidarr_api_key:/ {gsub(/"/,"",$2); print $2}')"
LIDARR=http://192.168.1.116:8686

# 1. Trigger a backup (POST /api/v1/command, CommandResource)
curl -fsS -X POST "$LIDARR/api/v1/command" \
  -H "X-Api-Key: $LIDARR_API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"Backup"}'

# 2. Confirm it landed (GET /api/v1/system/backup) — newest entry, type "manual"
curl -fsS "$LIDARR/api/v1/system/backup" -H "X-Api-Key: $LIDARR_API_KEY" \
  | python3 -c 'import sys,json; b=max(json.load(sys.stdin), key=lambda x: x["time"]); print(b["type"], b["time"], b["name"], b["size"])'
```

`tools/lidarr-triage apply` checks the same endpoint and refuses to run if the
newest backup is older than an hour.

## Restore

### Option A: Lidarr native backup (preferred)

Lidarr UI → System → Backup → pick the manual backup from before the change →
Restore. Lidarr restarts itself. Or, over the API, the same list above plus
`POST /api/v1/system/backup/restore/{id}`.

### Option B: PBS `host/k8s-configs`

Use when the PVC itself is damaged or the native backups are gone.

1. Stop Flux from fighting you, then stop Lidarr:
   ```bash
   flux suspend kustomization apps      # break-glass path (HRs pin suspend: false)
   kubectl -n media scale deploy/lidarr --replicas=0
   ```
2. On the PVE host, restore the subtree to a scratch dir (the token/env the
   nightly job uses lives in `/etc/proxmox-backup-client/k8s-configs.env`):
   ```bash
   set -a; . /etc/proxmox-backup-client/k8s-configs.env; set +a
   proxmox-backup-client snapshot list host/k8s-configs --repository "$PBS_REPOSITORY"
   proxmox-backup-client restore "host/k8s-configs/<snapshot-time>" k8s-configs.pxar \
     /root/lidarr-restore --pattern 'media/lidarr-config/**' --repository "$PBS_REPOSITORY"
   ```
3. Apply the **WAL copy rule** from [`k8s-migration.md`](k8s-migration.md).
   The order matters: checkpoint the *restored copy* first, while its `-wal`
   file is still next to it, or you lose transactions committed after the
   last checkpoint.
   ```bash
   cd /root/lidarr-restore/media/lidarr-config
   sqlite3 lidarr.db "PRAGMA wal_checkpoint(TRUNCATE);"   # expect 0|0|0
   D=/rpool/data/k8s-configs/media/lidarr-config
   rm -f "$D"/lidarr.db-wal "$D"/lidarr.db-shm            # stale live WAL/SHM
   cp lidarr.db "$D"/lidarr.db                            # the .db only
   ```
   Restore `config.xml` too if it's damaged.
4. Scale back up and resume:
   ```bash
   kubectl -n media scale deploy/lidarr --replicas=1
   kubectl -n media logs deploy/lidarr -c app | grep -i corrupt   # expect nothing
   flux resume kustomization apps
   ```

### Restoring deleted track files

Files are in `/Tank/media_root/lidarr-recycle/<artist folder>/…` until the
cleanup age passes. Move them back to their original path (the triage tool's
JSONL run log records every original path), then in Lidarr: artist → Refresh &
Scan. Re-monitor the album if you want upgrades again.

## Rollback of this change

This commit only adds `.gitignore` rules and this doc. The Recycling Bin is a
UI setting: clear it in Settings → Media Management to go back to permanent
deletes.
