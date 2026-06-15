#!/usr/bin/env bash
# backup-state.sh — Encrypt the Terraform state file and copy it to the NAS.
#
# terraform.tfstate is the only record of provisioned infrastructure, and it
# contains secrets (API tokens, generated PVE token values) in cleartext. It
# lives only on this workstation. This script keeps an encrypted off-box copy
# on the fileserver.
#
# Normally run automatically by the terraform-state-backup.path systemd unit
# whenever terraform.tfstate changes (i.e. after every `tofu apply`).
# Can also be run by hand.
#
# Encryption uses ansible-vault. Because systemd runs non-interactively, a
# vault password FILE is required — it cannot prompt. Create it once:
#
#   mkdir -p ~/.config/ansible-on-nest
#   printf '%s' 'YOUR_VAULT_PASSWORD' > ~/.config/ansible-on-nest/vault-pass
#   chmod 600 ~/.config/ansible-on-nest/vault-pass
#
# Override the location with ANSIBLE_VAULT_PASSWORD_FILE if you keep it elsewhere.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
STATE_FILE="$REPO_DIR/terraform/terraform.tfstate"

NAS_HOST="root@192.168.1.17"
NAS_DIR="/mnt/media_root/backups/terraform"
KEEP=30  # number of timestamped copies to retain on the NAS

SSH_KEY="$HOME/.ssh/ansible-on-nest"
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10 -i "$SSH_KEY")

VAULT_PASS_FILE="${ANSIBLE_VAULT_PASSWORD_FILE:-$HOME/.config/ansible-on-nest/vault-pass}"

log()  { echo "[backup-state] $*"; }
fail() { echo "[backup-state] ERROR: $*" >&2; exit 1; }

command -v ansible-vault >/dev/null || fail "ansible-vault not installed"
[[ -f "$STATE_FILE" ]] || fail "state file not found: $STATE_FILE"
[[ -f "$VAULT_PASS_FILE" ]] || fail "vault password file not found: $VAULT_PASS_FILE
  Create it once (see this script's header for details)."

stamp="$(date +%Y%m%d-%H%M%S)"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

log "encrypting $STATE_FILE"
ansible-vault encrypt --vault-password-file "$VAULT_PASS_FILE" \
    --output=- "$STATE_FILE" > "$tmp" \
    || fail "ansible-vault encrypt failed"

log "copying to $NAS_HOST:$NAS_DIR"
# shellcheck disable=SC2029  # $NAS_DIR is a trusted local config value, intended to expand client-side
ssh "${SSH_OPTS[@]}" "$NAS_HOST" "mkdir -p '$NAS_DIR'" \
    || fail "could not create $NAS_DIR on the NAS"
scp "${SSH_OPTS[@]}" "$tmp" "$NAS_HOST:$NAS_DIR/terraform.tfstate.$stamp.vault" \
    || fail "scp to NAS failed"
# shellcheck disable=SC2029  # $NAS_DIR/$stamp are trusted local values, intended to expand client-side
ssh "${SSH_OPTS[@]}" "$NAS_HOST" \
    "cp '$NAS_DIR/terraform.tfstate.$stamp.vault' '$NAS_DIR/terraform.tfstate.latest.vault'" \
    || fail "could not update latest copy"

log "pruning to the last $KEEP timestamped copies"
# shellcheck disable=SC2029  # $NAS_DIR/$KEEP are trusted local values, intended to expand client-side
ssh "${SSH_OPTS[@]}" "$NAS_HOST" \
    "ls -1t '$NAS_DIR'/terraform.tfstate.*.vault 2>/dev/null \
       | grep -v '\.latest\.vault\$' \
       | tail -n +$((KEEP + 1)) \
       | xargs -r rm -f" \
    || log "warning: prune step failed (backup itself succeeded)"

log "done — $NAS_DIR/terraform.tfstate.$stamp.vault"
