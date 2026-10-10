# Media apps: SQLite → Postgres

The *arrs, Bazarr and Seerr keep SQLite databases on the `nfs-nvme` PVCs.
SQLite's own docs say WAL mode doesn't work over a network filesystem, and it
shows here:
- Sonarr's `/ping` stalled 20s+ with `database is locked`, plus
  `database disk image is malformed` on 2026-10-07.
- Prowlarr has a `prowlarr.db.corrupt` from 2026-02.
- Lidarr has a 0-byte `lidarr-recovered.db`.

This doc moves each app that supports Postgres onto the shared `postgres`
service (18.6, alpha; operations in [postgres.md](postgres.md)). The rule is
zero rows lost, proven by a checker that doesn't trust the copy tool.

**Status (2026-10-10):**
- All six apps pass a rehearsal on PG18 (rehearsal log below).
- Credentials are generated and Flux-managed.
- Prod cutovers wait for the 72h gate: the shared Postgres has run clean on
  alpha since 2026-10-10 01:04 UTC, so the gate ends ~2026-10-13 01:04 UTC.
- Order: Prowlarr → Radarr → Sonarr → Bazarr → Seerr → Lidarr.

## Scope

| App | PG support | Migrates |
|---|---|---|
| Prowlarr, Radarr, Sonarr, Lidarr | Npgsql, PG 14–18 (Servarr wiki) | yes |
| Bazarr | `POSTGRES_*` env (wiki.bazarr.media) | yes |
| Seerr | `DB_TYPE=postgres` (seerr docs) | yes |
| Digarr | embedded PGlite, external PG via `DB_HOST` | no, trial app |
| Tunarr, Tdarr, SABnzbd, MediaLyze, Watcharr, Recommendarr, Watchback, Storyteller | none found | no |

Every one of these projects calls migrating an existing SQLite DB
**unsupported**. There's only a community pgloader recipe, and the Servarr wiki
says it was written against PG14 ("for 18+ start fresh"). The rehearsal below
is how we find out whether it works on 18 for each app. Prowlarr: it does.

## Shared host, one role per app

Each app gets a non-superuser login role that owns `<app>-main` and
`<app>-log` (Bazarr and Seerr: one DB each, `bazarr` and `seerr`), like
`jellyfin`.

Credentials already exist:
- `scripts/gen-pg-app-secrets.sh` generated `<app>_postgres_user` and
  `<app>_postgres_password` in `group_vars/all.sops.yaml` (#694).
- Flux applies them as `<app>-postgres-secret` from
  `k8s/apps/media/<app>-postgres-secret.sops.yaml` (docs/secrets.md).

The role and DBs on the server are created by hand at cutover.

The wiki says Prowlarr needs superuser for Housekeeping's VACUUM. Rehearsed
2026-10-10: Housekeeping completes as the owner role. Postgres only skips the
shared system catalogs (`permission denied to vacuum "pg_database", skipping
it`), and autovacuum handles those.

Before any app moves, `postgres` must run 72h on alpha with no OOMKilled.
After each app joins, watch peak memory and connection count.

## Tooling: `scripts/pg-migrate/`

- `migrate-local.sh <app> <src-dir> <image>` runs the whole pipeline on the
  workstation. On success it writes `out/<app>-{main,log}.dump` for prod.
  `--cleanup` as a 4th argument removes the containers.
- `verify.py` is the independent checker. `Dockerfile` builds its image
  (psycopg pinned).
- `sql-truncate-all.sql` and `sql-reset-sequences.sql`.

### What the pipeline does
1. **Fold the WAL.** Copy `db`, `-wal` and `-shm` together, then
   `sqlite3 copy.db ".backup clean.db"` and `PRAGMA journal_mode=DELETE`.
   - The wiki mounts only `<app>.db:ro`, which silently drops everything still
     in the WAL. Sonarr's was 4.3MB.
   - `integrity_check` must be `ok`, and per-table counts with the WAL applied
     must equal `clean.db`.
2. **Postgres 18.6 on a `--internal` Docker network.** A rehearsal *arr must
   never reach indexers or download clients.
3. **Schema-creation start** of the exact prod image (by digest). It's done
   when `VersionInfo` in both DBs has as many rows as SQLite's. A count
   mismatch means the image isn't the version that last wrote the DB.
4. **Load.**
   - `TRUNCATE` every table, seed rows included. The wiki's per-app `DELETE`
     lists predate Radarr 6 and the Lidarr plugins fork.
   - pgloader (`ghcr.io/dimitri/pgloader`, digest-pinned, upstream master)
     with `--with "quote identifiers" --with "data only"`. Any reject file or
     `ERROR` line fails the run: pgloader skips bad rows and keeps going.
   - Reset sequences.
   - The "casted to type bigserial … not the same as integer" warnings are
     expected: with data only, the app-created schema wins.
5. **`verify.py`** on main and log.
6. **`pg_dump -Fc`** both DBs to `out/`.

### What `verify.py` proves
- **Tables and columns.** The same table set and the same columns per table.
  A table only in Postgres must be empty. `VersionInfo` is compared in full,
  so the schema version matches.
- **Rows.** Every row is read on both sides, normalised by its Postgres
  column type and hashed. The two multisets of hashes must be equal. That
  checks exact count and content, independent of order or collation.
- **Normalisations:**
  - lossless: 0/1 ↔ bool, TEXT exact, BLOB ↔ bytea, NULL ≠ `''`, int ↔ bigint
  - `real` (float4) columns are compared at float32. Postgres sends
    `real` as its shortest text (`10.86`), and a naive double compare
    fails. Radarr's `MovieMetadata.Popularity` matched exactly: Radarr
    already stores it as a C# `float`. A SQLite double that float32 can't
    hold would be counted as lossy.
  - lossy, the one exception seen so far: the 7th fractional digit of a
    timestamp
- **Timestamps.** .NET writes 100ns ticks (`…:57.3697383Z`) and Postgres
  stores microseconds. Postgres derives the µs as `rint(strtod(frac) * 1e6)`,
  which is binary floating point, not decimal rounding: `.5054715 → .505471`,
  `.0637965 → .063797`. The checker reproduces that exactly
  (`pg_round_us`), so a timestamp off by even 1µs still fails. Values
  that lost their 7th digit are counted per column and reported.
- **Sequences.** Every owned sequence's next value is above `max(column)`.
  A sequence behind its table is what broke Jellyfin on 2026-09-30.
- **Output.** Failures name the row key and the differing columns only. Rows
  hold indexer API keys, so values print only with `--show-values`.

The checker was tested by planting faults in a migrated Prowlarr DB: a
deleted row, a changed text value, a flipped boolean, a timestamp moved
1µs, and a sequence set to 1. It caught all five.

## Rehearsal log

| Date | App | Image | Result |
|---|---|---|---|
| 2026-10-10 | Prowlarr | `linuxserver/prowlarr@sha256:f2b26429…` (2.6.5) | PASS: main 20 tables / 37,164 rows, log 3 / 8,242; 41,371 timestamps lost their 7th digit; API counts (indexers 5, apps 4, tags 2, history 36,791) equal SQLite; Housekeeping OK as owner role |
| 2026-10-10 | Radarr | `linuxserver/radarr@sha256:adb6c09d…` (6.4.4) | PASS: main 41 tables / 7,839 rows, log 3 / 4,021; API counts (movies 41, quality profiles 7, indexers 4, download clients 1, history 190) equal SQLite |
| 2026-10-10 | Sonarr | `linuxserver/sonarr@sha256:a5c1a5fe…` (4.0.20) | PASS: main 38 tables / 97,168 rows, log 3 / 19,218; API counts (series 114, quality profiles 7, indexers 3, download clients 2, history 31,741) equal SQLite; EpisodeFiles 107 = 107 |
| 2026-10-10 | Lidarr | `linuxserver-labs/prarr:lidarr-plugins@sha256:106b3bec…` (3.1.2.4913) | PASS: main 40 tables / 646,233 rows, log 3 / 23,214; API counts (artists 371, albums 8,660, history 23,553, quality profiles 3, indexers 4, root folders 1) equal SQLite. First run hit the pgloader heap limit; see gotchas |
| 2026-10-10 | Bazarr | `linuxserver/bazarr@sha256:8b30e81c…` (1.6.2) | PASS: 17 tables / 912 rows, 0 values lost precision; API series 114, movies 37 equal SQLite |
| 2026-10-10 | Seerr | `seerr-team/seerr@sha256:c92d2dc1…` (3.3.0) | PASS: 14 tables / 760 rows (`migrations` kept as Postgres created it; see gotchas); API users 1, requests 0 equal SQLite. The official pgloader image worked; the community `ralgar/pgloader` build the Seerr docs recommend wasn't needed |

## Prod cutover (per app)

Order: Prowlarr → Radarr → Sonarr → Bazarr → Seerr → Lidarr. While Radarr or
Sonarr is frozen, freeze Seerr too, so requests aren't sent to a missing app.

1. **Freeze PR.** Set `controllers.<app>.replicas: 0` (plus `seerr` when
   needed). Merge, and confirm the pod is gone.
2. **Rollback point (manual, PVE).**
   `zfs snapshot rpool/data/k8s-configs@pre-pg-<app>-<date>`.
   **Never `zfs rollback` this dataset.** It's one dataset holding the live
   Postgres data dir and every PVC, so a rollback rewinds Jellyfin, Mealie and
   every app already migrated. Restore single files from `.zfs/snapshot/`.
3. **Copy the quiescent files**, taken from the snapshot so nothing can still
   be writing: `/rpool/data/k8s-configs/.zfs/snapshot/<snap>/media/<app>-config/`.
   Run `migrate-local.sh` on them. It must PASS.
4. **Restore (manual).**
   - Create the role and DBs on prod `postgres`.
   - `pg_restore --no-owner --role=<app> -d <app>-main out/<app>-main.dump`,
     and the same for `-log`.
   - Run `verify.py` against prod (port-forward) and the same `clean-*.db`.
   - For Lidarr, watch etcd fsync on alpha-control: `nfs-nvme` shares the
     rpool with etcd.
5. **Cutover PR.** Set `<APP>__POSTGRES__{HOST,PORT,USER,PASSWORD,MAINDB,LOGDB}`
   from `<app>-postgres-secret`, and `replicas: 1`.
6. **Smoke test.**
   - Ready, clean logs, API counts equal SQLite's.
   - A manual search and an RSS sync work.
   - Prowlarr sync works, and Seerr can reach Radarr and Sonarr.
7. **Rollback window: 48h.** Revert the cutover PR and the app is back on its
   untouched SQLite. **Writes made on Postgres after cutover are lost on
   rollback.** There's no reverse migration. After 48h clean, destroy the
   snapshot and rename the old `.db` files to `*.pre-pg` on the PVE host.

Steps 2–4 are data operations outside GitOps. Each one is approved when it
runs, as with the Jellyfin repair.

## Gotchas found so far
- **PVE host reads of a live SQLite file can fail** with `Resource temporarily
  unavailable`, apparently because the NFS client holds a lease on it. For
  rehearsal, copy through the pod: `kubectl exec ... tar`. For cutover the
  app is stopped and the snapshot copy is used.
- **`.backup` keeps the WAL flag.** Without `journal_mode=DELETE` the clean
  copy won't open read-only.
- **The rehearsal app generates a new API key** in its throwaway
  `config.xml`. Prod keeps its own `config.xml` and key; the DB doesn't
  store it.
- **pgloader heap.** The pgloader image's Lisp runtime has a fixed 1GB heap.
  Lidarr's 437MB DB exhausted it with the default prefetch, and the run
  failed cleanly. The script now always uses `prefetch rows = 100` and
  `batch size = 1MB` (Servarr wiki tip).
- **Seerr keeps separate migration histories per database.** TypeORM ships 52
  SQLite and 18 Postgres migrations at v3.3.0. Seerr's own guide copies the
  `migrations` table anyway; we don't. `migrations` is a `KEEP_TABLES` entry:
  - not truncated, not loaded, and skipped by `verify.py --skip-tables`
  - "schema ready" means Postgres has recorded every Postgres migration the
    image ships (`/app/dist/migration/postgres`)
- **Bazarr and Seerr use one DB each,** named `bazarr` and `seerr`. The *arrs
  use two: `<app>-main` and `<app>-log`.
- **Seerr's old `jellyseerr/db/db.sqlite3`** is a leftover from the Jellyseerr
  rename, untouched since May. The live DB is `/app/config/db/db.sqlite3`.
