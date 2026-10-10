#!/usr/bin/env bash
# gen-pg-app-secrets.sh — Add Postgres credentials for apps on the shared
# media postgres to the ansible vault.
#
# For each app it adds <app>_postgres_user (= the app name) and
# <app>_postgres_password (48 random hex chars), but only if missing, so it's
# safe to rerun. Existing values are never changed and nothing is printed.
# playbooks/provision/k8s.yml turns them into <app>-postgres-secret; the role
# itself is created on postgres at cutover (docs/media-postgres-migration.md).
#
# Vault password: ANSIBLE_VAULT_PASSWORD_FILE, else
# ~/.config/ansible-on-nest/vault-pass (same as gen-mcp-secrets.sh).
#
# Usage: ./scripts/gen-pg-app-secrets.sh prowlarr radarr sonarr ...

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VAULT_FILE="$(dirname "$SCRIPT_DIR")/inventory/group_vars/all/vault.yml"
VAULT_PASS_FILE="${ANSIBLE_VAULT_PASSWORD_FILE:-$HOME/.config/ansible-on-nest/vault-pass}"

[[ $# -gt 0 ]] || { echo "usage: $0 <app>..." >&2; exit 2; }
for cmd in ansible-vault openssl; do
    command -v "$cmd" >/dev/null || { echo "required tool not found: $cmd" >&2; exit 1; }
done
[[ -f $VAULT_PASS_FILE ]] || { echo "vault password file not found: $VAULT_PASS_FILE" >&2; exit 1; }

# Plaintext only ever lives in a private temp dir, removed on exit.
TMP=$(mktemp -d)
chmod 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT

# ansible-vault refuses non-blocking stdio (e.g. some agent shells), so use
# files and /dev/null rather than pipes.
ansible-vault decrypt --vault-password-file "$VAULT_PASS_FILE" \
    --output "$TMP/vault.yml" "$VAULT_FILE" </dev/null >"$TMP/log" 2>&1 \
    || { cat "$TMP/log" >&2; exit 1; }

added=()
for app in "$@"; do
    [[ $app =~ ^[a-z][a-z0-9]*$ ]] || { echo "bad app name: $app" >&2; exit 2; }
    if ! grep -q "^${app}_postgres_user:" "$TMP/vault.yml"; then
        printf '%s_postgres_user: "%s"\n' "$app" "$app" >> "$TMP/vault.yml"
        added+=("${app}_postgres_user")
    fi
    if ! grep -q "^${app}_postgres_password:" "$TMP/vault.yml"; then
        printf '%s_postgres_password: "%s"\n' "$app" "$(openssl rand -hex 24)" >> "$TMP/vault.yml"
        added+=("${app}_postgres_password")
    fi
done

if [[ ${#added[@]} -eq 0 ]]; then
    echo "nothing to add; vault unchanged"
    exit 0
fi

ansible-vault encrypt --vault-password-file "$VAULT_PASS_FILE" --encrypt-vault-id default \
    --output "$TMP/vault.enc" "$TMP/vault.yml" </dev/null >"$TMP/log" 2>&1 \
    || { cat "$TMP/log" >&2; exit 1; }
cat "$TMP/vault.enc" > "$VAULT_FILE"
printf 'added to vault: %s\n' "${added[@]}"
