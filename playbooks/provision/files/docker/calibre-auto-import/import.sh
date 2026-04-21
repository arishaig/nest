#!/bin/bash
set -uo pipefail

CONFIG_DB="/calibre-web-config/app.db"
LIBRARY="/books"
WATCH_DIR="/imports"
SHELF_NAME="Synced to Kobo"
SHELF_USER_ID=1
EXTS_FIND=( -name '*.epub' -o -name '*.mobi' -o -name '*.azw*' -o -name '*.pdf' -o -name '*.cbz' -o -name '*.cbr' )

ensure_shelf() {
  python3 - <<PYEOF
import sqlite3, uuid
from datetime import datetime
conn = sqlite3.connect("${CONFIG_DB}")
cur = conn.cursor()
cur.execute("SELECT id FROM shelf WHERE name=? AND user_id=?", ("${SHELF_NAME}", ${SHELF_USER_ID}))
row = cur.fetchone()
if row is None:
    now = datetime.utcnow().isoformat(sep=' ', timespec='microseconds')
    cur.execute(
        "INSERT INTO shelf (uuid, name, is_public, user_id, kobo_sync, created, last_modified) "
        "VALUES (?, ?, 0, ?, 1, ?, ?)",
        (str(uuid.uuid4()), "${SHELF_NAME}", ${SHELF_USER_ID}, now, now)
    )
    conn.commit()
    print(f"[shelf] created '${SHELF_NAME}' (id={cur.lastrowid}, kobo_sync=1)")
else:
    print(f"[shelf] '${SHELF_NAME}' already exists (id={row[0]})")
conn.close()
PYEOF
}

add_to_shelf() {
  local ids="$1"
  python3 - "$ids" <<'PYEOF'
import sqlite3, sys
from datetime import datetime

CONFIG_DB = "/calibre-web-config/app.db"
SHELF_NAME = "Synced to Kobo"
SHELF_USER_ID = 1

ids = [int(x) for x in sys.argv[1].replace(' ', '').split(',') if x]
if not ids:
    sys.exit(0)

conn = sqlite3.connect(CONFIG_DB)
cur = conn.cursor()
cur.execute("SELECT id FROM shelf WHERE name=? AND user_id=?", (SHELF_NAME, SHELF_USER_ID))
row = cur.fetchone()
if row is None:
    print(f"[shelf] missing '{SHELF_NAME}' — skipping shelf insert", file=sys.stderr)
    sys.exit(1)
shelf_id = row[0]

now = datetime.utcnow().isoformat(sep=' ', timespec='microseconds')
for bid in ids:
    cur.execute("SELECT 1 FROM book_shelf_link WHERE book_id=? AND shelf=?", (bid, shelf_id))
    if cur.fetchone() is None:
        cur.execute(
            'INSERT INTO book_shelf_link (book_id, "order", shelf, date_added) VALUES (?, ?, ?, ?)',
            (bid, bid, shelf_id, now)
        )
        print(f"[shelf] book {bid} -> '{SHELF_NAME}'")
conn.commit()
conn.close()
PYEOF
}

# Install inotify-tools if not present (idempotent on container restart)
if ! command -v inotifywait >/dev/null 2>&1; then
  apt-get update && apt-get install -y inotify-tools
fi

ensure_shelf

echo "Calibre Auto-Import started. Watching ${WATCH_DIR} for new books..."
while inotifywait -e close_write,moved_to -r "${WATCH_DIR}"; do
  echo "New file detected, settling for 5s..."
  sleep 5
  if find "${WATCH_DIR}" -type f \( "${EXTS_FIND[@]}" \) 2>/dev/null | grep -q .; then
    echo "Importing books to Calibre library..."
    OUTPUT=$(calibredb add "${WATCH_DIR}/" --library-path="${LIBRARY}" --recurse 2>&1)
    echo "$OUTPUT"
    IDS=$(echo "$OUTPUT" | grep -oE 'Added book ids?: [0-9, ]+' | sed -E 's/Added book ids?: //' | tr -d ' \n' | sed 's/,$//' | paste -sd, -)
    if [ -n "${IDS}" ]; then
      add_to_shelf "${IDS}"
    else
      echo "No new book IDs (likely duplicates skipped)."
    fi
    echo "Cleaning up import folder..."
    find "${WATCH_DIR}" -type f \( "${EXTS_FIND[@]}" \) -delete
    echo "Ready for next import."
  fi
done
