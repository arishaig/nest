#!/usr/bin/env bash
# Rehearse (or perform the data half of) an app's SQLite -> Postgres
# migration entirely on this workstation. Nothing here touches the cluster.
#
#   migrate-local.sh <app> <src-dir> <image> [--cleanup]
#
#   app      prowlarr | radarr | sonarr | lidarr | bazarr | seerr
#   src-dir  directory holding the app's SQLite files (+ -wal/-shm):
#              *arrs   <app>.db and logs.db        (from /config)
#              bazarr  bazarr.db                   (from /config/db)
#              seerr   db.sqlite3                  (from /app/config/db)
#   image    the exact app image prod runs, by digest
#
# Steps (docs/media-postgres-migration.md):
#   1. fold the WAL into standalone clean-*.db files and integrity-check them
#   2. postgres:18.6 on an --internal network (no egress: a rehearsal app
#      must never reach indexers, download clients or Jellyfin)
#   3. start the app once against empty DBs so it creates its schema; stop it
#   4. TRUNCATE every table, pgloader data-only, reset sequences
#   5. verify.py on every DB; any difference fails the run
# On success it leaves pg_dump -Fc archives in <src-dir>/out/ for prod restore,
# and the containers running for inspection (`--cleanup` removes them).
set -euo pipefail

APP=${1:?app}; SRC=$(realpath "${2:?src-dir}"); IMAGE=${3:?image}
HERE=$(cd "$(dirname "$0")" && pwd)
PG_IMAGE=postgres:18.6
PGLOADER=ghcr.io/dimitri/pgloader@sha256:a1d4a78e78a64e46cd3fc7dfc57d24eb91ffb1a5520f2b1f55631815e3658d6e
NET=pgm-$APP; PG=pgm-$APP-pg; APPC=pgm-$APP-app
ROLE=$APP; PW=rehearsal
W=$SRC/work; OUT=$SRC/out

# Per app:
#   DBS          "<label>:<sqlite file>:<postgres db>" for each database
#   VERSION_SQL  query whose result must match between SQLite and Postgres
#                before the schema counts as created (works in both dialects)
#   APP_ENV      env vars pointing the app at the rehearsal postgres
#   CONFIG_MOUNT where the app keeps its config (an empty dir is mounted)
#   PGL_CAST     extra pgloader --cast options
#   KEEP_TABLES  app-owned tables left exactly as the app created them in
#                Postgres: not truncated, not loaded, not compared
#   PG_VERSION   optional command printing the Postgres schema version the
#                image should reach, when it differs from SQLite's
PGL_CAST=(); KEEP_TABLES=""; PG_VERSION=""
case $APP in
  prowlarr|radarr|sonarr|lidarr)
    P=$(tr '[:lower:]' '[:upper:]' <<<"$APP")__POSTGRES__
    DBS=("main:$APP.db:$APP-main" "log:logs.db:$APP-log")
    VERSION_SQL='SELECT count(*) FROM "VersionInfo"'
    APP_ENV=(PUID=1000 PGID=1000 TZ=UTC "${P}HOST=$PG" "${P}PORT=5432"
             "${P}USER=$ROLE" "${P}PASSWORD=$PW" "${P}MAINDB=$APP-main" "${P}LOGDB=$APP-log")
    CONFIG_MOUNT=/config ;;
  bazarr)
    DBS=("main:bazarr.db:bazarr")
    VERSION_SQL='SELECT version_num FROM alembic_version'
    APP_ENV=(PUID=1000 PGID=1000 TZ=UTC POSTGRES_ENABLED=true "POSTGRES_HOST=$PG"
             POSTGRES_PORT=5432 POSTGRES_DATABASE=bazarr "POSTGRES_USERNAME=$ROLE"
             "POSTGRES_PASSWORD=$PW")
    CONFIG_MOUNT=/config
    # Bazarr's wiki recipe: these columns are text in SQLite, timestamp in PG.
    for t in table_blacklist table_blacklist_movie table_history table_history_movie; do
      PGL_CAST+=(--cast "column $t.timestamp to timestamp")
    done ;;
  seerr)
    DBS=("main:db.sqlite3:seerr")
    VERSION_SQL='SELECT count(*) FROM migrations'
    APP_ENV=(TZ=UTC DB_TYPE=postgres "DB_HOST=$PG" DB_PORT=5432 "DB_USER=$ROLE"
             "DB_PASS=$PW" DB_NAME=seerr)
    CONFIG_MOUNT=/app/config
    # TypeORM keeps separate migration histories per database type (v3.3.0:
    # 52 SQLite, 18 Postgres), so SQLite's `migrations` rows must never be
    # copied in; the schema is ready once every Postgres migration the image
    # ships is recorded.
    KEEP_TABLES=migrations
    PG_VERSION="docker run --rm --entrypoint sh $IMAGE -c 'ls /app/dist/migration/postgres | grep -c \.js\$'" ;;
  *) echo "unsupported app $APP" >&2; exit 2 ;;
esac

log() { printf '\n== %s\n' "$*"; }
psql_c() { docker exec -i "$PG" psql -v ON_ERROR_STOP=1 -U admin -At "$@"; }

if [[ ${4:-} == --cleanup ]]; then
  docker rm -f "$APPC" "$PG" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  exit 0
fi

mkdir -p "$W" "$OUT"

log "1. fold WAL and integrity-check"
for spec in "${DBS[@]}"; do
  IFS=: read -r k f _ <<<"$spec"
  [[ -f $SRC/$f ]] || { echo "missing $SRC/$f" >&2; exit 1; }
  rm -f "$W/clean-$k.db"
  # Copy with the WAL next to it so SQLite replays it, then .backup writes a
  # standalone file. Feeding pgloader the bare .db would silently drop every
  # transaction still in the WAL.
  rm -rf "$W/copy-$k"; mkdir -p "$W/copy-$k"
  cp -p "$SRC/$f" "$W/copy-$k/"
  for ext in -wal -shm; do [[ -f $SRC/$f$ext ]] && cp -p "$SRC/$f$ext" "$W/copy-$k/"; done
  sqlite3 "$W/copy-$k/$f" ".backup '$W/clean-$k.db'"
  # .backup keeps the WAL flag; switch to a rollback journal so the file is
  # self-contained and opens read-only.
  sqlite3 "$W/clean-$k.db" "PRAGMA journal_mode=DELETE;" >/dev/null
  ic=$(sqlite3 "$W/clean-$k.db" "PRAGMA integrity_check;")
  [[ $ic == ok ]] || { echo "integrity_check $k: $ic" >&2; exit 1; }
  # Row counts with the WAL applied must equal the standalone copy.
  for t in $(sqlite3 "$W/clean-$k.db" "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"); do
    a=$(sqlite3 "$W/copy-$k/$f" "SELECT count(*) FROM \"$t\"")
    b=$(sqlite3 "$W/clean-$k.db" "SELECT count(*) FROM \"$t\"")
    [[ $a == "$b" ]] || { echo "$k.$t: $a rows with WAL, $b in clean copy" >&2; exit 1; }
  done
  echo "$k: integrity ok, row counts match"
done

log "2. postgres on an internal network"
docker rm -f "$APPC" "$PG" >/dev/null 2>&1 || true
docker network rm "$NET" >/dev/null 2>&1 || true
docker network create --internal "$NET" >/dev/null
docker run -d --name "$PG" --network "$NET" -e POSTGRES_USER=admin \
  -e POSTGRES_PASSWORD=rehearsal "$PG_IMAGE" >/dev/null
until docker exec "$PG" pg_isready -U admin -q 2>/dev/null; do sleep 1; done
sleep 2
# Same shape as prod: a non-superuser role owning every DB.
psql_c -d postgres -c "CREATE ROLE \"$ROLE\" LOGIN PASSWORD '$PW'"
for spec in "${DBS[@]}"; do
  IFS=: read -r _ _ db <<<"$spec"
  psql_c -d postgres -c "CREATE DATABASE \"$db\" OWNER \"$ROLE\""
done

log "3. schema-creation start of $IMAGE"
rm -rf "$W/config"; mkdir -p "$W/config"
chmod 777 "$W/config"  # images that run as a fixed non-root uid (seerr: node)
env_args=(); for e in "${APP_ENV[@]}"; do env_args+=(-e "$e"); done
docker run -d --name "$APPC" --network "$NET" "${env_args[@]}" \
  -v "$W/config:$CONFIG_MOUNT" "$IMAGE" >/dev/null
# Done when every DB carries the expected schema version: SQLite's, unless
# PG_VERSION says what the image's own Postgres history should reach.
want_version() {
  if [[ -n $PG_VERSION ]]; then bash -c "$PG_VERSION"; else sqlite3 "$W/clean-$1.db" "$VERSION_SQL"; fi
}
ready=0
for _ in $(seq 1 180); do
  ready=1
  for spec in "${DBS[@]}"; do
    IFS=: read -r k _ db <<<"$spec"
    want=$(want_version "$k")
    got=$(psql_c -d "$db" -c "$VERSION_SQL" 2>/dev/null || true)
    [[ $got == "$want" ]] || ready=0
  done
  [[ $ready == 1 ]] && break
  sleep 2
done
if [[ $ready != 1 ]]; then
  for spec in "${DBS[@]}"; do
    IFS=: read -r k _ db <<<"$spec"
    echo "schema version $k: postgres='$(psql_c -d "$db" -c "$VERSION_SQL" 2>/dev/null || true)' expected='$(want_version "$k")'" >&2
  done
  echo "The image must be the exact version that last wrote the SQLite DB." >&2
  docker logs --tail 50 "$APPC" >&2; exit 1
fi
sleep 10  # let startup tasks settle before stopping
docker stop -t 60 "$APPC" >/dev/null
echo "schema created at the expected version"

log "4. truncate, load, reset sequences"
for spec in "${DBS[@]}"; do
  IFS=: read -r k _ db <<<"$spec"
  { echo "SET pgm.keep_tables = '$KEEP_TABLES';"; cat "$HERE/sql-truncate-all.sql"; } \
    | psql_c -d "$db" -f -
  # Load from a copy without the KEEP_TABLES; verify.py still reads clean-$k.db.
  cp "$W/clean-$k.db" "$W/load-$k.db"
  for t in ${KEEP_TABLES//,/ }; do sqlite3 "$W/load-$k.db" "DROP TABLE IF EXISTS \"$t\""; done
  rm -rf "$W/pgloader-$k"; mkdir -p "$W/pgloader-$k"
  # Small prefetch/batch: the pgloader image's SBCL heap is fixed at 1GB and
  # Lidarr's 437MB DB exhausted it with the defaults (Servarr wiki tip).
  docker run --rm --network "$NET" -v "$W:/w" "$PGLOADER" pgloader \
    --root-dir "/w/pgloader-$k" --with "quote identifiers" --with "data only" \
    --with "prefetch rows = 100" --with "batch size = 1MB" "${PGL_CAST[@]}" \
    "/w/load-$k.db" "postgresql://$ROLE:$PW@$PG/$db" | tee "$W/pgloader-$k.log"
  # pgloader skips rows it can't load and carries on; any reject file fails us.
  if find "$W/pgloader-$k" -type f -name '*.dat' -size +0 | grep -q .; then
    echo "pgloader rejected rows for $k:" >&2
    find "$W/pgloader-$k" -type f -name '*.dat' -size +0 >&2; exit 1
  fi
  if grep -qiE '^\s*(ERROR|FATAL)' "$W/pgloader-$k.log"; then
    echo "pgloader logged errors for $k" >&2; exit 1
  fi
  psql_c -d "$db" -f - < "$HERE/sql-reset-sequences.sql"
done

log "5. verify"
docker build -q -t pgm-verify "$HERE" >/dev/null
for spec in "${DBS[@]}"; do
  IFS=: read -r k _ db <<<"$spec"
  echo "-- $k ($db)"
  docker run --rm --network "$NET" -v "$W:/w:ro" pgm-verify \
    "/w/clean-$k.db" "host=$PG dbname=$db user=$ROLE password=$PW" \
    ${KEEP_TABLES:+--skip-tables "$KEEP_TABLES"} | tee "$W/verify-$k.log"
done

log "6. export verified DBs for prod restore"
for spec in "${DBS[@]}"; do
  IFS=: read -r _ _ db <<<"$spec"
  docker exec "$PG" pg_dump -U admin -Fc --no-owner -d "$db" > "$OUT/$db.dump"
done
ls -la "$OUT"
echo
echo "PASS. Containers left up for inspection; '$0 $APP $2 $IMAGE --cleanup' removes them."
