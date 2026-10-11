# Shared Postgres (`media/postgres`)

One Postgres 18.6 instance in the `media` namespace serves Jellyfin and
Mealie, and the media apps as they move off SQLite
([media-postgres-migration.md](media-postgres-migration.md)). Manifest:
`k8s/apps/media/postgres.yaml`, Service `postgres:5432`, data on the
`postgres-data` PVC (`nfs-nvme`, PVE `rpool/data/k8s-configs/media/postgres-data`).

AudioMuse runs its own `audiomuse-postgres` (15) and Digarr embeds PGlite. Neither
is on this instance.

## Roles and databases

| Role | Kind | Owns | Used by | Secret |
|---|---|---|---|---|
| `mealie` | superuser (the image's `POSTGRES_USER`) | `mealie`, `postgres` | Mealie, postgres-exporter, the backup CronJob | `postgres-secret` |
| `jellyfin` | login, no superuser | `jellyfin` | Jellyfin | `jellyfin-postgres-secret` |
| `<app>` per media app | login, no superuser | `<app>-main` + `<app>-log` (Bazarr, Seerr: `bazarr`, `seerr`) | created at each app's cutover | `<app>-postgres-secret` |

The Secrets are SOPS-encrypted manifests that Flux applies, generated from
`playbooks/provision/k8s-secrets.yml` ([secrets.md](secrets.md)). To add an
app role: run `scripts/gen-pg-app-secrets.sh <app>`, add its entry to
`k8s-secrets.yml`, and run `scripts/render-k8s-secrets.sh`. The role and
database themselves are created by hand at cutover (see the migration doc).

## Placement and settings

| Setting | Value | Why |
|---|---|---|
| Node | hard-pinned to alpha (`workloads=general`) | It was OOMKilled 17× in 6 days on gamma-rpi5 (2026-10), and the Pi had no headroom. Jellyfin can only run on alpha anyway, so this adds no new failure domain. Never on omega. |
| Memory | request 1Gi, limit 4Gi | Jellyfin backends reach ~250MB anon RSS each on big library queries, outside Postgres's own memory contexts. 1Gi ran out with a handful running at once. |
| `jit` | `off` | Jellyfin's EF-generated queries (dozens of subqueries) cross `jit_above_cost`; LLVM compiling them is pure overhead for OLTP. |
| `max_connections` | 200 | Room for the *arrs, which each pool a main and a log DB |
| `shared_buffers` | 512MB | Fits under the 4Gi limit |

All of these are `args` in `postgres.yaml` (#691, #692). After any change,
watch for `OOMKilled`:
`kubectl -n media get pod -l app.kubernetes.io/name=postgres -o jsonpath='{..lastState}'`.

## Backups

The PBS host backup of `rpool/data/k8s-configs` (03:00, `playbooks/provision/pbs.yml`)
copies files with no snapshot. A **live PGDATA copied that way is not
guaranteed to restore**, so it isn't the Postgres backup. The logical dumps are:

- **What:** the `postgres-backup` CronJob (`k8s/apps/media/postgres-backup.yaml`),
  daily at 02:30 America/Los_Angeles, pinned to alpha.
- **Writes:** `pg_dumpall --globals-only > globals.sql` (roles) and a `pg_dump -Fc`
  per database, then `COMPLETE`.
- **Where:** `backups/<YYYYmmdd-HHMMSS>/` on the `postgres-data` PVC, outside
  PGDATA. On PVE that's
  `/rpool/data/k8s-configs/media/postgres-data/backups/`. The 03:00 PBS run
  picks it up.
- **Checks:** `pg_restore -l` on every archive; an unreadable dump fails the
  job.
- **Keeps:** 7 days on the PVC.
- **Run one now:** `kubectl -n media create job --from=cronjob/postgres-backup pb-manual`.

PBS is the only copy beyond the PVC: there is **no offsite copy**
([disaster-recovery.md](disaster-recovery.md)).

## Restore

Pick the newest `backups/<ts>/` that has a `COMPLETE` marker. From inside the
`postgres` pod (or any `postgres:18` client with the superuser's credentials):

```sh
B=/var/lib/postgresql/backups/<ts>
# Roles first; ignore "already exists" for roles that survived.
psql -d postgres -f "$B/globals.sql"
# Whole database lost: recreate it from the dump.
pg_restore -d postgres --create "$B/jellyfin.dump"
# Database exists but its contents are bad: replace them in place.
pg_restore -d jellyfin --clean --if-exists "$B/jellyfin.dump"
```

Stop the app first (`replicas: 0` through git) so nothing writes during the
restore. To test a dump without touching prod, restore it into a scratch
database on a local `postgres:18.6` container, as `scripts/pg-migrate/` does.

The dump format was tested on 2026-10-10 against a throwaway `postgres:18.6`:
the dumps were written, pruned, and restored with the right row count. A
restore test of a real nightly dump is still to do.

## Monitoring

`postgres-exporter` (on gamma, `192.168.1.116:9187` via the metrics LB) logs
in as `mealie`. Restarts and OOMKills show in the pod's `lastState`, as above.
