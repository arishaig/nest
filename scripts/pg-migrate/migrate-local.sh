#!/usr/bin/env bash
# Rehearse (or perform the data half of) an *arr SQLite -> Postgres migration
# entirely on this workstation. Nothing here touches the cluster.
#
#   migrate-local.sh <app> <src-dir> <image>
#
#   app      prowlarr | radarr | sonarr | lidarr
#   src-dir  directory holding <app>.db (+ -wal/-shm) and logs.db (+ -wal/-shm),
#            copied from /rpool/data/k8s-configs/media/<app>-config on PVE
#   image    the exact app image prod runs, ideally by digest
#
# Steps (docs/media-postgres-migration.md):
#   1. fold the WAL into standalone clean-*.db files and integrity-check them
#   2. postgres:18.6 on an --internal network (no egress: a rehearsal *arr
#      must never reach indexers or download clients)
#   3. start the app once against empty DBs so it creates its schema; stop it
#   4. TRUNCATE every table, pgloader data-only, reset sequences
#   5. verify.py on both DBs; any difference fails the run
# On success it leaves pg_dump -Fc archives in <src-dir>/out/ for prod restore,
# and the containers running for inspection (`--cleanup` removes them).
set -euo pipefail

APP=${1:?app}; SRC=$(realpath "${2:?src-dir}"); IMAGE=${3:?image}
HERE=$(cd "$(dirname "$0")" && pwd)
PG_IMAGE=postgres:18.6
PGLOADER=ghcr.io/dimitri/pgloader@sha256:a1d4a78e78a64e46cd3fc7dfc57d24eb91ffb1a5520f2b1f55631815e3658d6e
NET=pgm-$APP; PG=pgm-$APP-pg; APPC=pgm-$APP-app
ROLE=$APP; PW=rehearsal; MAIN=$APP-main; LOG=$APP-log
ENVP=$(tr '[:lower:]' '[:upper:]' <<<"$APP")__POSTGRES__
W=$SRC/work; OUT=$SRC/out

case $APP in prowlarr|radarr|sonarr|lidarr) ;; *) echo "unsupported app $APP" >&2; exit 2;; esac

log() { printf '\n== %s\n' "$*"; }
psql_c() { docker exec -i "$PG" psql -v ON_ERROR_STOP=1 -U admin -At "$@"; }

if [[ ${4:-} == --cleanup ]]; then
  docker rm -f "$APPC" "$PG" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  exit 0
fi

mkdir -p "$W" "$OUT"
# The app and pgloader write as root inside containers; keep their output in W.

log "1. fold WAL and integrity-check"
for pair in "$APP.db:main" "logs.db:log"; do
  f=${pair%%:*}; k=${pair##*:}
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
# Same shape as prod: a non-superuser role owning both DBs.
psql_c -d postgres -c "CREATE ROLE \"$ROLE\" LOGIN PASSWORD '$PW'"
psql_c -d postgres -c "CREATE DATABASE \"$MAIN\" OWNER \"$ROLE\""
psql_c -d postgres -c "CREATE DATABASE \"$LOG\" OWNER \"$ROLE\""

log "3. schema-creation start of $IMAGE"
mkdir -p "$W/config"
docker run -d --name "$APPC" --network "$NET" \
  -e PUID=1000 -e PGID=1000 -e TZ=UTC \
  -e "${ENVP}HOST=$PG" -e "${ENVP}PORT=5432" -e "${ENVP}USER=$ROLE" \
  -e "${ENVP}PASSWORD=$PW" -e "${ENVP}MAINDB=$MAIN" -e "${ENVP}LOGDB=$LOG" \
  -v "$W/config:/config" "$IMAGE" >/dev/null
# Done when both DBs carry the same migration history as SQLite.
want_main=$(sqlite3 "$W/clean-main.db" 'SELECT count(*) FROM "VersionInfo"')
want_log=$(sqlite3 "$W/clean-log.db" 'SELECT count(*) FROM "VersionInfo"')
for _ in $(seq 1 180); do
  got_main=$(psql_c -d "$MAIN" -c 'SELECT count(*) FROM "VersionInfo"' 2>/dev/null || echo 0)
  got_log=$(psql_c -d "$LOG" -c 'SELECT count(*) FROM "VersionInfo"' 2>/dev/null || echo 0)
  [[ $got_main == "$want_main" && $got_log == "$want_log" ]] && break
  sleep 2
done
if [[ $got_main != "$want_main" || $got_log != "$want_log" ]]; then
  echo "schema version mismatch: main $got_main/$want_main log $got_log/$want_log" >&2
  echo "The image must be the exact version that last wrote the SQLite DB." >&2
  docker logs --tail 50 "$APPC" >&2; exit 1
fi
sleep 10  # let startup tasks settle before stopping
docker stop -t 60 "$APPC" >/dev/null
echo "schema created (VersionInfo main=$got_main log=$got_log)"

log "4. truncate, load, reset sequences"
for pair in "main:$MAIN" "log:$LOG"; do
  k=${pair%%:*}; db=${pair##*:}
  psql_c -d "$db" -f - < "$HERE/sql-truncate-all.sql"
  rm -rf "$W/pgloader-$k"; mkdir -p "$W/pgloader-$k"
  docker run --rm --network "$NET" -v "$W:/w" "$PGLOADER" pgloader \
    --root-dir "/w/pgloader-$k" --with "quote identifiers" --with "data only" \
    "/w/clean-$k.db" "postgresql://$ROLE:$PW@$PG/$db" | tee "$W/pgloader-$k.log"
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
for pair in "main:$MAIN" "log:$LOG"; do
  k=${pair%%:*}; db=${pair##*:}
  echo "-- $k"
  docker run --rm --network "$NET" -v "$W:/w:ro" pgm-verify \
    "/w/clean-$k.db" "host=$PG dbname=$db user=$ROLE password=$PW" | tee "$W/verify-$k.log"
done

log "6. export verified DBs for prod restore"
for db in "$MAIN" "$LOG"; do
  docker exec "$PG" pg_dump -U admin -Fc --no-owner -d "$db" > "$OUT/$db.dump"
done
ls -la "$OUT"
echo
echo "PASS. Containers left up for inspection; '$0 $APP $2 $IMAGE --cleanup' removes them."
