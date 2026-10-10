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
# Encrypted with age to the same two recipients as every SOPS file (the
# `recipients` anchor in .sops.yaml: the working age key and the Bitwarden-held
# SSH recovery key; docs/secrets.md). Encrypting needs only public keys, so
# no secret is required here. Restore with either key:
#
#   age -d -i ~/.config/sops/age/keys.txt terraform.tfstate.latest.age > terraform.tfstate
#   age -d -i ./nest-sops-recovery         terraform.tfstate.latest.age > terraform.tfstate
#
# Copies made before 2026-10 are *.vault (ansible-vault); they need the old
# vault password and are left on the NAS, not pruned.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
STATE_FILE="$REPO_DIR/terraform/terraform.tfstate"

NAS_HOST="root@192.168.1.17"
NAS_DIR="/mnt/media_root/backups/terraform"
KEEP=30  # number of timestamped copies to retain on the NAS

SSH_KEY="$HOME/.ssh/ansible-on-nest"
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10 -i "$SSH_KEY")


log()  { echo "[backup-state] $*"; }
fail() { echo "[backup-state] ERROR: $*" >&2; exit 1; }

command -v age >/dev/null || fail "age not installed"
command -v yq >/dev/null || fail "yq not installed"
[[ -f "$STATE_FILE" ]] || fail "state file not found: $STATE_FILE"

# One recipient per line, from the anchor every SOPS rule uses.
recipients=$(yq -r '.recipients' "$REPO_DIR/.sops.yaml" | tr ',' '\n' | sed 's/^ *//; /^$/d')
[[ $(wc -l <<<"$recipients") -ge 2 ]] || fail "expected 2 recipients in .sops.yaml, got: $recipients"

stamp="$(date +%Y%m%d-%H%M%S)"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

log "encrypting $STATE_FILE"
age -R <(printf '%s\n' "$recipients") -o "$tmp" "$STATE_FILE" \
    || fail "age encrypt failed"

log "copying to $NAS_HOST:$NAS_DIR"
# shellcheck disable=SC2029  # $NAS_DIR is a trusted local config value, intended to expand client-side
ssh "${SSH_OPTS[@]}" "$NAS_HOST" "mkdir -p '$NAS_DIR'" \
    || fail "could not create $NAS_DIR on the NAS"
scp "${SSH_OPTS[@]}" "$tmp" "$NAS_HOST:$NAS_DIR/terraform.tfstate.$stamp.age" \
    || fail "scp to NAS failed"
# shellcheck disable=SC2029  # $NAS_DIR/$stamp are trusted local values, intended to expand client-side
ssh "${SSH_OPTS[@]}" "$NAS_HOST" \
    "cp '$NAS_DIR/terraform.tfstate.$stamp.age' '$NAS_DIR/terraform.tfstate.latest.age'" \
    || fail "could not update latest copy"

log "pruning to the last $KEEP timestamped copies"
# shellcheck disable=SC2029  # $NAS_DIR/$KEEP are trusted local values, intended to expand client-side
ssh "${SSH_OPTS[@]}" "$NAS_HOST" \
    "ls -1t '$NAS_DIR'/terraform.tfstate.*.age 2>/dev/null \
       | grep -v '\.latest\.age\$' \
       | tail -n +$((KEEP + 1)) \
       | xargs -r rm -f" \
    || log "warning: prune step failed (backup itself succeeded)"

log "done — $NAS_DIR/terraform.tfstate.$stamp.age"
