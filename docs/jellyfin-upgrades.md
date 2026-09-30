# Jellyfin upgrades (Postgres fork)

Jellyfin runs `ghcr.io/jpvenson/jellyfin.pgsql`
([JPVenson/Jellyfin.Pgsql](https://github.com/JPVenson/Jellyfin.Pgsql)). This
is Jellyfin plus a plugin that stores the library in Postgres (the shared
`postgres` service, DB `jellyfin`) instead of SQLite. Schema changes come from
the plugin's EF Core migrations, which run at startup.

## How a failed upgrade behaves

On startup Jellyfin:
1. `pg_dump`s the DB to `/config/data/PgsqlBackups/`;
2. applies pending migrations;
3. restores that dump if a migration fails.

The pod never becomes ready, so the liveness probe restarts it and the cycle
repeats. **Roll back by reverting the image tag**, not by touching the DB.

Up to and including `10.11.11-1`, step 3 is **not all-or-nothing**
(upstream #49, fixed in `12.1-2`). It restores `__EFMigrationsHistory` but can
leave new columns, indexes and FKs behind, and still logs "restore completed
successfully". A later upgrade then fails on objects that "already exist".

## 2026-09-30: 10.11.8 → 10.11.11 blocked, repaired

The 10.11.11 bump (#641) failed with
`42701: column "NormalizedUsername" of relation "Users" already exists`, and
was reverted (#642). The DB was already out of step with its migration history:
- `Users.NormalizedUsername` + `IX_Users_NormalizedUsername` existed, but
  `20260522092303_AddNormalizedUsername` wasn't in the history. Where they
  came from is unknown; they were already there before #641. An earlier
  non-atomic restore is the likely cause.
- Six ID sequences were behind `max(Id)` (ActivityLogs, ApiKeys,
  CustomItemDisplayPreferences, DisplayPreferences, HomeSection, ImageInfos).
  This caused `PK_ActivityLogs` duplicate-key errors.

This blocked 12.1 as well, since the migration chain runs through the same
10.11.11 steps. The fix was a manual one-off on prod, after a `pg_dump`. 10.11.8
never reads the column:

```sql
\set ON_ERROR_STOP on
BEGIN;
DROP INDEX IF EXISTS public."IX_Users_NormalizedUsername";
ALTER TABLE public."Users" DROP COLUMN IF EXISTS "NormalizedUsername";
DO $$ DECLARE r record; mx bigint; lv bigint; BEGIN
  FOR r IN SELECT s.relname seq, t.relname tbl, a.attname col
           FROM pg_class s JOIN pg_depend d ON d.objid = s.oid
           JOIN pg_class t ON t.oid = d.refobjid
           JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
           WHERE s.relkind = 'S' LOOP
    EXECUTE format('SELECT max(%I) FROM public.%I', r.col, r.tbl) INTO mx;
    EXECUTE format('SELECT last_value FROM public.%I', r.seq) INTO lv;
    IF mx IS NOT NULL AND mx > lv THEN
      PERFORM setval(format('public.%I', r.seq), mx);
    END IF;
  END LOOP;
END $$;
COMMIT;
```

## Before any Jellyfin bump: rehearse offline

Run the new image against a copy of the DB on a workstation with Docker. Don't
use the cluster for this.

```sh
# 1. Copy prod (read-only). The dump holds user data; delete it afterwards.
P=$(kubectl -n media get pod -l app.kubernetes.io/name=postgres -o name)
kubectl -n media exec $P -- sh -c 'pg_dump -U "$POSTGRES_USER" -d jellyfin -Fc' > jellyfin.dump

# 2. Local Postgres, same major as prod (k8s/apps/media/postgres.yaml).
docker network create jfr
docker run -d --name jfr-pg --network jfr -e POSTGRES_USER=mealie \
  -e POSTGRES_PASSWORD=rehearsal -e POSTGRES_DB=jellyfin -v "$PWD:/d:ro" postgres:18.6
docker exec jfr-pg pg_restore -U mealie -d jellyfin --no-owner --role=mealie --exit-on-error /d/jellyfin.dump

# 3. New image, empty config. Expect "Startup complete", then check
#    __EFMigrationsHistory, row counts and /health.
docker run -d --name jfr-jf --network jfr -e POSTGRES_HOST=jfr-pg -e POSTGRES_PORT=5432 \
  -e POSTGRES_DB=jellyfin -e POSTGRES_USER=mealie -e POSTGRES_PASSWORD=rehearsal \
  -v "$PWD/cfg:/config" ghcr.io/jpvenson/jellyfin.pgsql:<new-tag>

# 4. Rollback check: start the *current* tag against the upgraded DB.
# 5. Clean up.
docker rm -f jfr-jf jfr-pg && docker network rm jfr && rm -rf jellyfin.dump cfg
```

The rehearsal uses an empty `/config`, so it doesn't exercise prod's plugins.
Before merging a bump, check each installed plugin's target ABI against the
new version.

For the 10.11.11 retry, the unrepaired DB reproduced the 42701 failure. The
repaired DB applied `AddNormalizedUsername` → `UpdateNormalizedUsername` →
`AddUniqueNormalizedUsernameIndex` and started in 7 s. `10.11.8-1` then also
started cleanly on the upgraded DB.

## Prod rollout checklist

1. `pg_dump -Fc` the `jellyfin` DB right before merging. Keep it off-cluster.
2. Merge. Watch `kubectl -n media logs deploy/jellyfin -f` for
   `Perform migration` / `Startup complete` / `FTL`.
3. On failure: revert the tag. If the DB is left inconsistent (pre-12.1-2
   restore), restore the dump yourself in one transaction:
   drop and recreate `public`, then `pg_restore --single-transaction
   --exit-on-error`.
4. After success: remove the stray backups in `/config/data/PgsqlBackups/`.
