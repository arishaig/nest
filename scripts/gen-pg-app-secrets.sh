#!/usr/bin/env bash
# gen-pg-app-secrets.sh — Add Postgres credentials for apps on the shared
# media postgres to inventory/group_vars/all.sops.yaml.
#
# For each app it adds <app>_postgres_user (= the app name) and
# <app>_postgres_password (48 random hex chars), but only if missing, so it's
# safe to rerun. Existing values are never changed and nothing is printed.
# playbooks/provision/k8s.yml turns them into <app>-postgres-secret; the role
# itself is created on postgres at cutover (docs/media-postgres-migration.md).
#
# Decrypts/encrypts with the age key (docs/secrets.md). `sops set` edits one
# key in place, so the git diff shows exactly the keys added.
#
# Usage: ./scripts/gen-pg-app-secrets.sh prowlarr radarr sonarr ...

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SECRETS="inventory/group_vars/all.sops.yaml"

[[ $# -gt 0 ]] || { echo "usage: $0 <app>..." >&2; exit 2; }
for cmd in sops openssl; do
    command -v "$cmd" >/dev/null || { echo "required tool not found: $cmd" >&2; exit 1; }
done
cd "$REPO_DIR"

has_key() { sops decrypt --extract "[\"$1\"]" "$SECRETS" >/dev/null 2>&1; }

added=()
for app in "$@"; do
    [[ $app =~ ^[a-z][a-z0-9]*$ ]] || { echo "bad app name: $app" >&2; exit 2; }
    if ! has_key "${app}_postgres_user"; then
        sops set "$SECRETS" "[\"${app}_postgres_user\"]" "\"$app\""
        added+=("${app}_postgres_user")
    fi
    if ! has_key "${app}_postgres_password"; then
        sops set "$SECRETS" "[\"${app}_postgres_password\"]" "\"$(openssl rand -hex 24)\""
        added+=("${app}_postgres_password")
    fi
done

if [[ ${#added[@]} -eq 0 ]]; then
    echo "nothing to add; secrets unchanged"
else
    printf 'added: %s\n' "${added[@]}"
fi
