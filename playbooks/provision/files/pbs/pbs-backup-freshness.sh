#!/usr/bin/env bash
# pbs-backup-freshness.sh — Emit Prometheus textfile metrics for PBS snapshot age.
#
# Writes the Unix timestamp of the newest snapshot per backup group to a .prom
# file read by node_exporter's textfile collector. Prometheus then alerts
# (PBSBackupStale) when a guest has no recent backup.
#
# Installed and scheduled by playbooks/provision/pbs.yml.

set -euo pipefail

DATASTORE=/mnt/backup
TEXTFILE_DIR=/var/lib/node_exporter/textfile_collector
OUT="$TEXTFILE_DIR/pbs_backup.prom"

# Write to a temp file in the same dir, then atomically rename — the textfile
# collector must never see a half-written file. The temp name does not end in
# .prom, so node_exporter ignores it until the rename.
tmp="$(mktemp "$OUT.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

{
  echo "# HELP pbs_backup_last_snapshot_timestamp_seconds Unix time of the newest PBS snapshot for a backup group."
  echo "# TYPE pbs_backup_last_snapshot_timestamp_seconds gauge"
  for typedir in "$DATASTORE"/ct "$DATASTORE"/vm "$DATASTORE"/host; do
    [ -d "$typedir" ] || continue
    gtype="$(basename "$typedir")"
    for group in "$typedir"/*; do
      [ -d "$group" ] || continue
      gid="$(basename "$group")"
      newest="$(ls -1 "$group" 2>/dev/null | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}T' | sort | tail -1 || true)"
      [ -n "$newest" ] || continue
      epoch="$(date -d "$newest" +%s 2>/dev/null || true)"
      [ -n "$epoch" ] || continue
      echo "pbs_backup_last_snapshot_timestamp_seconds{guest_type=\"$gtype\",guest=\"$gid\"} $epoch"
    done
  done
} > "$tmp"

chmod 0644 "$tmp"
mv "$tmp" "$OUT"
