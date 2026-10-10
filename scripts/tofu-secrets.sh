#!/usr/bin/env bash
# tofu-secrets.sh — Print OpenTofu's secret variables as a tfvars file, read
# from inventory/group_vars/all.sops.yaml (docs/secrets.md, phase 3).
#
# There is no separate tofu secrets file: every value lives once, in the SOPS
# file, under the key its other consumers already use. The map below names
# which SOPS key feeds which tofu variable.
#
# Usage (plaintext only ever goes to a file you choose, or a pipe):
#   tofu -chdir=terraform plan -var-file=<(scripts/tofu-secrets.sh)
#   scripts/tofu-secrets.sh > "$RUNNER_TEMP/secrets.tfvars"   # CI
#
# Output is HCL with JSON-escaped strings, so `${`/`%{` are escaped and no
# value can be interpolated.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SECRETS="$(dirname "$SCRIPT_DIR")/inventory/group_vars/all.sops.yaml"

command -v sops >/dev/null || { echo "sops not installed" >&2; exit 1; }

# shellcheck disable=SC2016  # the Python program is meant to be single-quoted
sops decrypt "$SECRETS" | python3 -c '
import json, sys, yaml

# tofu variable -> key in group_vars/all.sops.yaml
MAP = {
    "pve_api_token":    "tofu_pve_api_token",       # terraform@pam!terraform
    "adguard_username": "adguard_admin_username",
    "adguard_password": "adguard_admin_password",
    "pbs_password":     "pbs_password",
    "cf_api_token":     "traefik_cf_dns_api_token",  # DNS-edit token (not cf_api_token)
    "vultr_api_key":    "vultr_api_key",
}

secrets = yaml.safe_load(sys.stdin)
missing = [k for k in MAP.values() if not isinstance(secrets.get(k), str) or not secrets[k]]
if missing:
    sys.exit(f"missing or empty in all.sops.yaml: {missing}")
for var, key in MAP.items():
    val = json.dumps(secrets[key]).replace("${", "$${").replace("%{", "%%{")
    print(f"{var} = {val}")
'
