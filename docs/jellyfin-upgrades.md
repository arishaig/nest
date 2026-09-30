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

From `12.1-2` the restore drops and recreates `public`, then loads the dump in
one transaction. Rehearsal showed it exact: same rows, FKs and indexes.
Afterwards `public` is owned by `mealie` and has no `USAGE` grant to PUBLIC.
Jellyfin doesn't care, since it connects as `mealie`.

The restore only covers the DB. Config-file changes made by migrations that
ran before the failure stay. For example, `DisableLegacyAuthorization` sets
`EnableLegacyAuthorization=false` in `system.xml`.

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

## 2026-09-30: 10.11.11 → 12.1 (`12.1-2`)

Stock `jellyfin/jellyfin:12.1` has two core migration routines that fail on
Postgres. The fork doesn't patch Jellyfin core (upstream fork #47):
- `20260910120000_MigrateRatingLevels` fails with `A command is already in
  progress`. It streams a `Distinct()` while running `ExecuteUpdate`, which
  Npgsql can't do. Any library with a rating hits it.
- `20260911120000_StripEmbeddedLinkedChildren` fails with `42883`, because it
  runs SQLite's `json_valid`/`json_remove`.

Core routines are recorded in `__EFMigrationsHistory` like EF migrations, so
we pre-insert them and Jellyfin skips them. We also skip
`DisableLegacyAuthorization`, which keeps `X-Emby-Token`/`api_key` clients
working; retiring legacy auth is a separate job. Run this before the first
12.1 start. 10.11.11 ignores the extra rows (rehearsed):

```sql
\set ON_ERROR_STOP on
INSERT INTO "__EFMigrationsHistory" ("MigrationId","ProductVersion") VALUES
  ('20260531160000_DisableLegacyAuthorization','12.1.0.0'),
  ('20260910120000_MigrateRatingLevels','12.1.0.0'),
  ('20260911120000_StripEmbeddedLinkedChildren','12.1.0.0');
```

What skipping costs:
- `InheritedParentalRatingValue` keeps its 10.11 values. That only matters
  for users with a max parental rating.
- Dead keys stay in the `Data` blobs until each item is next saved.

**Every later 12.x bump needs the same check.** `release-12.z` already has
another `MigrateRatingLevels` copy (`20260915120000`). Before each bump, list
the new routines in `Jellyfin.Server/Migrations/Routines/` and look for the
same patterns.

There is **no downgrade**: 12.1 converts columns to `uuid` and adds the
`LinkedChildren` table and FKs. To go back, restore the pre-upgrade dump (see
the checklist) and replace `/config/plugins`.

What the 12.x routines did to our DB in rehearsal:
- merged 126 case-only duplicate MusicArtists;
- moved 20 playlists' children into `LinkedChildren`;
- refreshed 6.4k `CleanName`s;
- **deleted no items** (`MigrateLinkedChildren`: "No stale items found").
It took 20 s.

Plugins: Jellyfin disables the 10.11 builds of Intro Skipper and Chapter
Segments Provider and loads the rest. The "Update Plugins" task then installs
the 12 builds (AudioMuse 0.3.5, Intro Skipper 12.0.4, Chapter Creator 0.6.1,
Chapter Segments 5.0, LrcLib 5.0, Webhook 22.0, and the 12 ABI builds of File
Transformation and MediaDash). After one restart, all of them are Active.
ListenBrainz 6.5.3.4 stays; it supports 12.

### `MigrateLinkedChildren` deletes items whose files are missing

On first start, 12.x removes every non-folder item whose file is missing
under a library root. It also removes items outside every root, but only
while all roots are reachable. Library roots come from the `.mblink` files in
`/config/root/default/*/`. **If `/config/root` is missing, there are no
roots, so every media item counts as "outside every root" and is deleted.**

An early rehearsal with only `config/` and `plugins/` copied lost 15,212
items this way. That was the rehearsal copy, not prod. So:
- Rehearse with `/config/root` included, and give the container a
  `/data/media` holding an empty placeholder file at each item path (below).
- Before the prod bump, check that every item path exists in the pod. Expect
  0 missing:

```sh
P=$(kubectl -n media get pod -l app.kubernetes.io/name=postgres -o name)
kubectl -n media exec $P -- sh -c 'psql -U "$POSTGRES_USER" -d jellyfin -At -c "select \"Path\" from \"BaseItems\" where \"Path\" like '"'"'/data/media/%'"'"' and not \"IsFolder\" and not \"IsVirtualItem\""' > paths.txt
kubectl -n media exec -i deploy/jellyfin -- sh -c 'while IFS= read -r p; do [ -e "$p" ] || echo "MISSING $p"; done' < paths.txt | wc -l
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

# 3. Prod's config, root (library roots!) and plugins. Leave out
#    database.xml (the entrypoint writes it from env) and
#    plugins/configurations (holds the ListenBrainz token and Webhook targets).
mkdir cfg && kubectl -n media exec deploy/jellyfin -- tar -C /config -cf - \
  --exclude=config/database.xml --exclude=plugins/configurations \
  config root plugins | tar -C cfg -xf -

# 4. Placeholder media tree, so library roots look like prod's.
kubectl -n media exec $P -- sh -c 'psql -U "$POSTGRES_USER" -d jellyfin -At -c "select \"Path\" from \"BaseItems\" where \"Path\" like '"'"'/data/media/%'"'"' and not \"IsFolder\" and not \"IsVirtualItem\""' \
  | sed 's#^/data/media/##' | while IFS= read -r p; do mkdir -p "media/$(dirname "$p")"; : > "media/$p"; done

# 5. New image, pinned by digest. Expect "Startup complete", then check
#    __EFMigrationsHistory, per-Type BaseItems counts, UserData, /health,
#    and /Items listings.
docker run -d --name jfr-jf --network jfr -e POSTGRES_HOST=jfr-pg -e POSTGRES_PORT=5432 \
  -e POSTGRES_DB=jellyfin -e POSTGRES_USER=mealie -e POSTGRES_PASSWORD=rehearsal \
  -v "$PWD/cfg:/config" -v "$PWD/media:/data/media:ro" ghcr.io/jpvenson/jellyfin.pgsql:<new-tag>@<digest>

# 6. Rollback check: start the *current* tag against the upgraded DB. If the
#    new version can't be downgraded from, restore the dump instead
#    (checklist step 3), then start the current tag.
# 7. Clean up. The container writes as root, so remove cfg through docker.
docker rm -f jfr-jf jfr-pg && docker network rm jfr
docker run --rm --entrypoint rm -v "$PWD:/w" postgres:18.6 -rf /w/cfg /w/media
rm -f jellyfin.dump
```

Stop the rehearsal container soon after checking. With the library roots
readable, a library scan would start probing the empty placeholder files.

For the 10.11.11 retry, the unrepaired DB reproduced the 42701 failure. The
repaired DB applied `AddNormalizedUsername` → `UpdateNormalizedUsername` →
`AddUniqueNormalizedUsernameIndex` and started in 7 s. `10.11.8-1` then also
started cleanly on the upgraded DB.

## Prod rollout checklist

1. `pg_dump -Fc` the `jellyfin` DB right before merging, and tar
   `/config/config` + `/config/plugins`. Keep both off-cluster.
2. Apply any pre-seed SQL for the version (see the 12.1 section), and run the
   missing-files check.
3. Merge. Watch `kubectl -n media logs deploy/jellyfin -f` for
   `Perform migration` / `Startup complete` / `FTL`. The startup probe allows
   15 min before liveness can restart the pod.
4. On failure: revert the tag. If the DB is left inconsistent (pre-12.1-2
   restore, or a version with no downgrade path), restore the dump yourself
   in one transaction:
   ```sql
   DROP SCHEMA public CASCADE;
   CREATE SCHEMA public AUTHORIZATION pg_database_owner;
   GRANT USAGE ON SCHEMA public TO PUBLIC;
   ```
   then `pg_restore --no-owner --role=mealie --single-transaction
   --exit-on-error`. **Replace** `/config/plugins` from the tar rather than
   unpacking over it, because newer-ABI plugin folders would otherwise stay.
5. After success:
   - restart once, so the updated plugins load;
   - check that every plugin is Active;
   - remove the stray backups in `/config/data/PgsqlBackups/`.
