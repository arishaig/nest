#!/usr/bin/env bash
# Store the Google Drive OAuth client + token for the offsite backup in SOPS
# (inventory/group_vars/all.sops.yaml). Prompts silently so nothing lands in
# shell history. See docs/offsite-backup.md for how to obtain the values.
set -euo pipefail
cd "$(dirname "$0")/.."
file=inventory/group_vars/all.sops.yaml

read -r -s -p "OAuth client_id: " client_id; echo
read -r -s -p "OAuth client_secret: " client_secret; echo
echo "Paste the token JSON printed by 'rclone authorize \"drive\"' (one line, then Enter):"
read -r -s token; echo

for v in client_id client_secret token; do
  [ -n "${!v}" ] || { echo "$v is empty" >&2; exit 1; }
done
printf '%s' "$token" | python3 -c 'import json,sys; json.load(sys.stdin)' \
  || { echo "token is not valid JSON" >&2; exit 1; }

set_key() {
  sops set "$file" "[\"$1\"]" "$(printf '%s' "$2" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
}
set_key vault_rclone_drive_client_id "$client_id"
set_key vault_rclone_drive_client_secret "$client_secret"
set_key vault_rclone_drive_token "$token"
echo "Stored. Commit $file and let the pve deploy run, then follow docs/offsite-backup.md."
