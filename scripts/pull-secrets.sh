#!/usr/bin/env bash
# pull-secrets.sh — Extracts secrets from live infrastructure and writes vault.yml
#
# Run this locally on a machine with SSH access to all hosts.
# It will create inventory/group_vars/all/vault.yml with real values.
#
# Usage: ./scripts/pull-secrets.sh
#
# Prerequisites:
#   - SSH access to all hosts (same key as ansible)
#   - jq installed locally
#   - yq installed locally (https://github.com/mikefarah/yq)

set -euo pipefail

# --- Config ---
SSH_KEY="$HOME/.ssh/ansible-on-nest"
SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=5 -i "$SSH_KEY")

DOCKER_HOST="root@192.168.1.158"
MONITORING_HOST="root@192.168.1.44"
SEEDBOX_HOST="root@192.168.1.182"
MUSICBRAINZ_HOST="root@192.168.1.197"
PBS_HOST="root@192.168.1.113"
ADGUARD_HOST="adguard@192.168.7.7"
FILESERVER_HOST="root@192.168.1.17"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
VAULT_FILE="$REPO_DIR/inventory/group_vars/all/vault.yml"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
fail()  { echo -e "${RED}[-]${NC} $*"; }

ssh_cmd() {
    local host="$1"; shift
    # shellcheck disable=SC2029  # remote command ("$@") is built from trusted local args by design
    ssh "${SSH_OPTS[@]}" "$host" "$@" 2>/dev/null
}

# Helper: safely extract a value, return CHANGEME if it fails
safe() {
    local val="$1"
    if [[ -z "$val" || "$val" == "null" ]]; then
        echo "CHANGEME"
    else
        echo "$val"
    fi
}

# --- Start ---
echo "============================================"
echo " Secret Extraction Script"
echo " $(date)"
echo "============================================"
echo ""
echo "This will pull secrets from your infrastructure"
echo "and write them to: $VAULT_FILE"
echo ""
read -r -p "Continue? [y/N] " confirm
[[ "$confirm" =~ ^[Yy]$ ]] || exit 0

# Check dependencies
for cmd in jq yq ssh; do
    if ! command -v "$cmd" &>/dev/null; then
        fail "Required command '$cmd' not found. Please install it."
        exit 1
    fi
done

echo ""

# =====================================================================
# Load existing vault values (skip anything already populated)
# =====================================================================
declare -A EXISTING=()
if [[ -f "$VAULT_FILE" ]]; then
    info "Loading existing vault values from $VAULT_FILE ..."

    # Determine if the vault file is encrypted
    VAULT_CONTENT=""
    if head -1 "$VAULT_FILE" | grep -q '^[$]ANSIBLE_VAULT'; then
        info "  Vault file is encrypted, decrypting..."
        VAULT_CONTENT=$(ansible-vault decrypt --output=- "$VAULT_FILE" 2>/dev/null) || {
            warn "  Could not decrypt vault file — will re-extract all secrets"
            VAULT_CONTENT=""
        }
    else
        VAULT_CONTENT=$(cat "$VAULT_FILE")
    fi

    if [[ -n "$VAULT_CONTENT" ]]; then
        while IFS= read -r line; do
            [[ "$line" =~ ^[[:space:]]*# ]] && continue
            [[ "$line" =~ ^[[:space:]]*$ ]] && continue
            [[ "$line" =~ ^[[:space:]]*- ]] && continue
            # Standard format: key: "value" or key: 'value'
            if [[ "$line" =~ ^([a-zA-Z_][a-zA-Z0-9_]*):\ *(.*) ]]; then
                key="${BASH_REMATCH[1]}"
                val="${BASH_REMATCH[2]}"
                # Strip surrounding quotes
                val="${val#\"}" ; val="${val%\"}"
                val="${val#\"}" ; val="${val%\"}"
                val="${val#\'}" ; val="${val%\'}"
                if [[ -n "$val" && "$val" != "CHANGEME" && "$val" != "null" && "$val" != '""' ]]; then
                    EXISTING["$key"]="$val"
                fi
            fi
        done <<< "$VAULT_CONTENT"
    fi

    info "  Found ${#EXISTING[@]} existing values — these will be kept"
    echo ""
fi

# Helper: return existing vault value if present, empty string otherwise
existing() {
    echo "${EXISTING[$1]:-}"
}

# Helper: use vault value if present, otherwise use extracted value (always returns 0)
or_existing() {
    local vault_key="$1"
    local extracted="$2"
    local cached="${EXISTING[$vault_key]:-}"
    if [[ -n "$cached" ]]; then
        echo "$cached"
    else
        echo "$extracted"
    fi
}

# Helper: prompt only if no existing value
prompt_or_existing() {
    local key="$1"
    local prompt_text="$2"
    local cached="${EXISTING[$key]:-}"
    if [[ -n "$cached" ]]; then
        info "  $prompt_text: already in vault, skipping" >&2
        echo "$cached"
    else
        read -r -p "  $prompt_text: " val
        safe "${val:-}"
    fi
}

# =====================================================================
# Cloudflare — these must be provided manually (not on any host)
# =====================================================================
info "Cloudflare credentials"
echo "  (The tunnel/Traefik/DDNS tokens are pulled from the Docker LXC .env)"
CF_API_TOKEN=$(prompt_or_existing "cf_api_token" "Cloudflare API Token (read-only, for audit)")
CF_ACCOUNT_ID=$(prompt_or_existing "cf_account_id" "Cloudflare Account ID (from dashboard URL)")

# Check if ALL keys for a host are already in the vault
all_cached() {
    for key in "$@"; do
        [[ -z "${EXISTING[$key]:-}" ]] && return 1
    done
    return 0
}

# Log helper: report result for a key
log_result() {
    local label="$1" val="$2"
    if [[ -n "$val" && "$val" != "CHANGEME" ]]; then
        info "  $label: ok"
    else
        warn "  $label: not found"
    fi
}

# =====================================================================
# Docker LXC — all secrets from /mnt/app_config/.env
# =====================================================================
DOCKER_KEYS=(cf_tunnel_token traefik_cf_dns_api_token cf_ddns_api_token
    homarr_secret_key mealie_postgres_user mealie_postgres_password
    authelia_session_secret authelia_storage_encryption_key authelia_jwt_secret
    sonarr_api_key radarr_api_key lidarr_api_key prowlarr_api_key bazarr_api_key
    jellyseerr_api_key recyclarr_sonarr_api_key recyclarr_radarr_api_key)

if all_cached "${DOCKER_KEYS[@]}"; then
    info "Docker LXC ($DOCKER_HOST): all values in vault, skipping SSH"
    CF_TUNNEL_TOKEN=$(existing "cf_tunnel_token")
    TRAEFIK_CF_TOKEN=$(existing "traefik_cf_dns_api_token")
    CF_DDNS_TOKEN=$(existing "cf_ddns_api_token")
    HOMARR_SECRET=$(existing "homarr_secret_key")
    MEALIE_PG_USER=$(existing "mealie_postgres_user")
    MEALIE_PG_PASS=$(existing "mealie_postgres_password")
    AUTHELIA_SESSION=$(existing "authelia_session_secret")
    AUTHELIA_STORAGE_ENC=$(existing "authelia_storage_encryption_key")
    AUTHELIA_JWT=$(existing "authelia_jwt_secret")
    SONARR_API_KEY=$(existing "sonarr_api_key")
    RADARR_API_KEY=$(existing "radarr_api_key")
    LIDARR_API_KEY=$(existing "lidarr_api_key")
    PROWLARR_API_KEY=$(existing "prowlarr_api_key")
    BAZARR_API_KEY=$(existing "bazarr_api_key")
    JELLYSEERR_API_KEY=$(existing "jellyseerr_api_key")
    RECYCLARR_SONARR_KEY=$(existing "recyclarr_sonarr_api_key")
    RECYCLARR_RADARR_KEY=$(existing "recyclarr_radarr_api_key")
else
    info "Pulling secrets from Docker LXC ($DOCKER_HOST)..."

    # Read the main .env file — this is where most secrets live
    DOCKER_ENV=$(ssh_cmd "$DOCKER_HOST" "cat /mnt/app_config/.env 2>/dev/null") || true

    if [[ -n "$DOCKER_ENV" ]]; then
        info "  /mnt/app_config/.env: found ($(echo "$DOCKER_ENV" | wc -l) lines)"

        # Helper to extract a value from the env file
        env_val() {
            echo "$DOCKER_ENV" | grep -E "^${1}=" | head -1 | cut -d= -f2- | tr -d '"'
        }

        # For each key: use vault if present, otherwise extract from .env
        CF_TUNNEL_TOKEN=$(or_existing "cf_tunnel_token" "$(env_val CLOUDFLARE_TUNNEL_TOKEN)")
        TRAEFIK_CF_TOKEN=$(or_existing "traefik_cf_dns_api_token" "$(env_val CLOUDFLARE_TRAEFIK_API_TOKEN)")
        CF_DDNS_TOKEN=$(or_existing "cf_ddns_api_token" "$(env_val CLOUDFLARE_DYNAMIC_DNS_API_TOKEN)")
        HOMARR_SECRET=$(or_existing "homarr_secret_key" "$(env_val HOMARR_SECRET_KEY)")
        MEALIE_PG_USER=$(or_existing "mealie_postgres_user" "$(env_val POSTGRES_USER)")
        MEALIE_PG_PASS=$(or_existing "mealie_postgres_password" "$(env_val POSTGRES_PASSWORD)")
        AUTHELIA_SESSION=$(or_existing "authelia_session_secret" "$(env_val AUTHELIA_SESSION_SECRET)")
        AUTHELIA_STORAGE_ENC=$(or_existing "authelia_storage_encryption_key" "$(env_val AUTHELIA_STORAGE_ENCRYPTION_KEY)")
        AUTHELIA_JWT=$(or_existing "authelia_jwt_secret" "$(env_val AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET)")
        SONARR_API_KEY=$(or_existing "sonarr_api_key" "$(env_val API_KEY_SONARR)")
        RADARR_API_KEY=$(or_existing "radarr_api_key" "$(env_val API_KEY_RADARR)")
        LIDARR_API_KEY=$(or_existing "lidarr_api_key" "$(env_val API_KEY_LIDARR)")
        PROWLARR_API_KEY=$(or_existing "prowlarr_api_key" "$(env_val API_KEY_PROWLARR)")
        BAZARR_API_KEY=$(or_existing "bazarr_api_key" "$(env_val API_KEY_BAZARR)")

        log_result "Cloudflare tunnel token" "$CF_TUNNEL_TOKEN"
        log_result "Traefik CF DNS token" "$TRAEFIK_CF_TOKEN"
        log_result "Cloudflare DDNS token" "$CF_DDNS_TOKEN"
        log_result "Homarr secret key" "$HOMARR_SECRET"
        log_result "Mealie Postgres" "$MEALIE_PG_PASS"
        log_result "Authelia secrets" "$AUTHELIA_SESSION"
        for svc in SONARR RADARR LIDARR PROWLARR BAZARR; do
            val_name="${svc}_API_KEY"
            log_result "${svc,,} API key" "${!val_name}"
        done
    else
        warn "  /mnt/app_config/.env: SSH failed or file not found!"
        # Fall back to vault for everything
        CF_TUNNEL_TOKEN=$(existing "cf_tunnel_token")
        TRAEFIK_CF_TOKEN=$(existing "traefik_cf_dns_api_token")
        CF_DDNS_TOKEN=$(existing "cf_ddns_api_token")
        HOMARR_SECRET=$(existing "homarr_secret_key")
        MEALIE_PG_USER=$(existing "mealie_postgres_user")
        MEALIE_PG_PASS=$(existing "mealie_postgres_password")
        AUTHELIA_SESSION=$(existing "authelia_session_secret")
        AUTHELIA_STORAGE_ENC=$(existing "authelia_storage_encryption_key")
        AUTHELIA_JWT=$(existing "authelia_jwt_secret")
        SONARR_API_KEY=$(existing "sonarr_api_key")
        RADARR_API_KEY=$(existing "radarr_api_key")
        LIDARR_API_KEY=$(existing "lidarr_api_key")
        PROWLARR_API_KEY=$(existing "prowlarr_api_key")
        BAZARR_API_KEY=$(existing "bazarr_api_key")
    fi

    # Seerr — not in .env, lives in container config
    JELLYSEERR_API_KEY=$(or_existing "jellyseerr_api_key" "$(ssh_cmd "$DOCKER_HOST" "docker exec seerr cat /app/config/settings.json 2>/dev/null" | jq -r '.main.apiKey // empty' 2>/dev/null || true)")
    log_result "jellyseerr API key" "$JELLYSEERR_API_KEY"

    # Recyclarr API keys
    RECYCLARR_SONARR_KEY=$(existing "recyclarr_sonarr_api_key")
    RECYCLARR_RADARR_KEY=$(existing "recyclarr_radarr_api_key")
    if [[ -z "$RECYCLARR_SONARR_KEY" || -z "$RECYCLARR_RADARR_KEY" ]]; then
        RECYCLARR_CFG=$(ssh_cmd "$DOCKER_HOST" "cat /mnt/app_config/recyclarr/recyclarr.yml 2>/dev/null") || true
        if [[ -n "$RECYCLARR_CFG" ]]; then
            [[ -z "$RECYCLARR_SONARR_KEY" ]] && RECYCLARR_SONARR_KEY=$(echo "$RECYCLARR_CFG" | yq '.. | select(has("api_key")) | .api_key' 2>/dev/null | head -1) || true
            [[ -z "$RECYCLARR_RADARR_KEY" ]] && RECYCLARR_RADARR_KEY=$(echo "$RECYCLARR_CFG" | yq '.. | select(has("api_key")) | .api_key' 2>/dev/null | tail -1) || true
            info "  Recyclarr keys: extracted"
        else
            warn "  Recyclarr: config not found"
        fi
    else
        info "  Recyclarr keys: in vault"
    fi
fi

# =====================================================================
# Monitoring LXC — PVE exporter, UnPoller, Grafana, Alertmanager
# =====================================================================
MONITORING_KEYS=(pve_api_token_id pve_api_token_secret unpoller_unifi_user
    unpoller_unifi_pass grafana_admin_password alertmanager_webhook_url)

if all_cached "${MONITORING_KEYS[@]}"; then
    info "Monitoring LXC ($MONITORING_HOST): all values in vault, skipping SSH"
    PVE_TOKEN_ID=$(existing "pve_api_token_id")
    PVE_TOKEN_SECRET=$(existing "pve_api_token_secret")
    UNPOLLER_USER=$(existing "unpoller_unifi_user")
    UNPOLLER_PASS=$(existing "unpoller_unifi_pass")
    GRAFANA_PW=$(existing "grafana_admin_password")
    ALERTMANAGER_WEBHOOK=$(existing "alertmanager_webhook_url")
else
    info "Pulling secrets from Monitoring LXC ($MONITORING_HOST)..."

    # Read the monitoring .env file
    MONITORING_ENV=$(ssh_cmd "$MONITORING_HOST" "cat /opt/monitoring/.env 2>/dev/null") || true

    # PVE Exporter
    PVE_TOKEN_ID=$(existing "pve_api_token_id")
    PVE_TOKEN_SECRET=$(existing "pve_api_token_secret")
    if [[ -z "$PVE_TOKEN_ID" || -z "$PVE_TOKEN_SECRET" ]]; then
        PVE_EXPORTER_CFG=$(ssh_cmd "$MONITORING_HOST" "cat /opt/monitoring/pve/pve.yml /opt/monitoring/pve-exporter/pve.yml 2>/dev/null") || true
        if [[ -n "$PVE_EXPORTER_CFG" ]]; then
            [[ -z "$PVE_TOKEN_ID" ]] && PVE_TOKEN_ID=$(echo "$PVE_EXPORTER_CFG" | yq '.default.user // ""' 2>/dev/null) || true
            [[ -z "$PVE_TOKEN_SECRET" ]] && PVE_TOKEN_SECRET=$(echo "$PVE_EXPORTER_CFG" | yq '.default.token_value // .default.password // ""' 2>/dev/null) || true
            info "  PVE exporter creds: extracted"
        else
            warn "  PVE exporter: config not found"
        fi
    else
        info "  PVE exporter creds: in vault"
    fi

    # UnPoller
    UNPOLLER_USER=$(existing "unpoller_unifi_user")
    UNPOLLER_PASS=$(existing "unpoller_unifi_pass")
    if [[ -z "$UNPOLLER_USER" || -z "$UNPOLLER_PASS" ]]; then
        UNPOLLER_CFG=$(ssh_cmd "$MONITORING_HOST" "cat /opt/monitoring/unpoller/up.conf 2>/dev/null") || true
        if [[ -n "$UNPOLLER_CFG" ]]; then
            [[ -z "$UNPOLLER_USER" ]] && UNPOLLER_USER=$(echo "$UNPOLLER_CFG" | grep -iP '^\s*user\s*=' | head -1 | sed 's/.*=\s*//' | tr -d '"') || true
            [[ -z "$UNPOLLER_PASS" ]] && UNPOLLER_PASS=$(echo "$UNPOLLER_CFG" | grep -iP '^\s*pass\s*=' | head -1 | sed 's/.*=\s*//' | tr -d '"') || true
            info "  UnPoller creds: extracted"
        else
            warn "  UnPoller: config not found"
        fi
    else
        info "  UnPoller creds: in vault"
    fi

    # Grafana admin password — try .env file, then docker env
    GRAFANA_PW=$(existing "grafana_admin_password")
    if [[ -z "$GRAFANA_PW" ]]; then
        # Try from .env file
        GRAFANA_PW=$(echo "$MONITORING_ENV" | grep -E '^GF_SECURITY_ADMIN_PASSWORD=' | head -1 | cut -d= -f2- | tr -d '"') || true
        # Try from grafana container env
        if [[ -z "$GRAFANA_PW" ]]; then
            GRAFANA_PW=$(ssh_cmd "$MONITORING_HOST" "docker inspect grafana --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null" | grep '^GF_SECURITY_ADMIN_PASSWORD=' | cut -d= -f2-) || true
        fi
    fi
    log_result "Grafana admin password" "$GRAFANA_PW"

    # Alertmanager webhook URL — try config file, then docker env
    ALERTMANAGER_WEBHOOK=$(existing "alertmanager_webhook_url")
    if [[ -z "$ALERTMANAGER_WEBHOOK" ]]; then
        ALERTMANAGER_CFG=$(ssh_cmd "$MONITORING_HOST" "cat /opt/monitoring/alertmanager/alertmanager.yml 2>/dev/null") || true
        if [[ -n "$ALERTMANAGER_CFG" ]]; then
            # Try multiple yq approaches for different alertmanager config formats
            ALERTMANAGER_WEBHOOK=$(echo "$ALERTMANAGER_CFG" | yq '.receivers[].webhook_configs[0].url // ""' 2>/dev/null | head -1) || true
            [[ -z "$ALERTMANAGER_WEBHOOK" ]] && ALERTMANAGER_WEBHOOK=$(echo "$ALERTMANAGER_CFG" | grep -oP 'url:\s*\K\S+' | head -1) || true
        fi
    fi
    log_result "Alertmanager webhook" "$ALERTMANAGER_WEBHOOK"
fi

# =====================================================================
# Seedbox LXC — VPN credentials
# =====================================================================
SEEDBOX_KEYS=(protonvpn_private_key gluetun_control_server_key)

if all_cached "${SEEDBOX_KEYS[@]}"; then
    info "Seedbox LXC ($SEEDBOX_HOST): all values in vault, skipping SSH"
    PROTONVPN_KEY=$(existing "protonvpn_private_key")
    GLUETUN_KEY=$(existing "gluetun_control_server_key")
else
    info "Pulling secrets from Seedbox LXC ($SEEDBOX_HOST)..."
    SEEDBOX_ENV=$(ssh_cmd "$SEEDBOX_HOST" "cat /opt/seedbox/.env 2>/dev/null") || true
    PROTONVPN_KEY=$(or_existing "protonvpn_private_key" "$(echo "$SEEDBOX_ENV" | grep -iE '^WIREGUARD_PRIVATE_KEY=|^VPN_PRIVATE_KEY=|^PRIVATE_KEY=' | head -1 | cut -d= -f2- | tr -d '"')")
    log_result "ProtonVPN WireGuard key" "$PROTONVPN_KEY"

    GLUETUN_KEY=$(or_existing "gluetun_control_server_key" "$(echo "$SEEDBOX_ENV" | grep -E '^GLUETUN_CONTROL_SERVER_KEY=' | head -1 | cut -d= -f2- | tr -d '"')")
    log_result "Gluetun control server key" "$GLUETUN_KEY"
fi

# =====================================================================
# MusicBrainz LXC — postgres password
# =====================================================================
MUSICBRAINZ_PG_PASS=$(existing "musicbrainz_postgres_password")
if [[ -n "$MUSICBRAINZ_PG_PASS" ]]; then
    info "MusicBrainz LXC ($MUSICBRAINZ_HOST): postgres password in vault, skipping SSH"
else
    info "Pulling secrets from MusicBrainz LXC ($MUSICBRAINZ_HOST)..."
    MUSICBRAINZ_PG_PASS=$(ssh_cmd "$MUSICBRAINZ_HOST" "cat /home/svc_musicbrainz/musicbrainz-docker/default/postgres.env 2>/dev/null" | grep -E '^POSTGRES_PASSWORD=' | head -1 | cut -d= -f2- | tr -d '"') || true
    log_result "MusicBrainz postgres password" "$MUSICBRAINZ_PG_PASS"
fi

# =====================================================================
# Monitoring LXC — Home Assistant bearer token (from prometheus.yml)
# =====================================================================
HA_BEARER_TOKEN=$(existing "homeassistant_bearer_token")
if [[ -n "$HA_BEARER_TOKEN" ]]; then
    info "Home Assistant bearer token: in vault, skipping"
else
    info "Pulling Home Assistant bearer token from Monitoring LXC ($MONITORING_HOST)..."
    HA_BEARER_TOKEN=$(ssh_cmd "$MONITORING_HOST" "cat /opt/monitoring/prometheus/prometheus.yml 2>/dev/null" | grep -A0 'bearer_token:' | head -1 | sed 's/.*bearer_token:\s*//' | tr -d ' "'"'" ) || true
    log_result "Home Assistant bearer token" "$HA_BEARER_TOKEN"
fi

# =====================================================================
# PBS — rclone Google Drive credentials
# =====================================================================
PBS_KEYS=(rclone_gdrive_client_id rclone_gdrive_client_secret rclone_gdrive_token)

if all_cached "${PBS_KEYS[@]}"; then
    info "PBS ($PBS_HOST): all values in vault, skipping SSH"
    RCLONE_CLIENT_ID=$(existing "rclone_gdrive_client_id")
    RCLONE_CLIENT_SECRET=$(existing "rclone_gdrive_client_secret")
    RCLONE_TOKEN=$(existing "rclone_gdrive_token")
else
    info "Pulling secrets from PBS ($PBS_HOST)..."
    RCLONE_CLIENT_ID=$(existing "rclone_gdrive_client_id")
    RCLONE_CLIENT_SECRET=$(existing "rclone_gdrive_client_secret")
    RCLONE_TOKEN=$(existing "rclone_gdrive_token")

    if [[ -z "$RCLONE_CLIENT_ID" || -z "$RCLONE_CLIENT_SECRET" || -z "$RCLONE_TOKEN" ]]; then
        RCLONE_CFG=$(ssh_cmd "$PBS_HOST" "cat /root/.config/rclone/rclone.conf 2>/dev/null") || true
        if [[ -n "$RCLONE_CFG" ]]; then
            [[ -z "$RCLONE_CLIENT_ID" ]] && RCLONE_CLIENT_ID=$(echo "$RCLONE_CFG" | grep -iP '^client_id\s*=' | head -1 | sed 's/.*=\s*//') || true
            [[ -z "$RCLONE_CLIENT_SECRET" ]] && RCLONE_CLIENT_SECRET=$(echo "$RCLONE_CFG" | grep -iP '^client_secret\s*=' | head -1 | sed 's/.*=\s*//') || true
            [[ -z "$RCLONE_TOKEN" ]] && RCLONE_TOKEN=$(echo "$RCLONE_CFG" | grep -iP '^token\s*=' | head -1 | sed 's/.*=\s*//') || true
            info "  rclone Google Drive creds: extracted"
        else
            warn "  rclone: config not found"
        fi
    else
        info "  rclone creds: in vault"
    fi
fi

# =====================================================================
# AdGuard Pi — admin password hash, plaintext password, DNS-edit CF token, certbot email
# =====================================================================
ADGUARD_KEYS=(adguard_admin_password_hash adguard_admin_password cf_dns_edit_api_token certbot_email)

if all_cached "${ADGUARD_KEYS[@]}"; then
    info "AdGuard Pi ($ADGUARD_HOST): all values in vault, skipping SSH"
    ADGUARD_HASH=$(existing "adguard_admin_password_hash")
    ADGUARD_PASSWORD=$(existing "adguard_admin_password")
    CF_DNS_EDIT_TOKEN=$(existing "cf_dns_edit_api_token")
    CERTBOT_EMAIL=$(existing "certbot_email")
else
    info "Pulling secrets from AdGuard Pi ($ADGUARD_HOST)..."

    ADGUARD_HASH=$(or_existing "adguard_admin_password_hash" "$(ssh_cmd "$ADGUARD_HOST" "sudo cat /opt/AdGuardHome/AdGuardHome.yaml 2>/dev/null" | yq '.users[0].password // ""' 2>/dev/null || true)")
    log_result "AdGuard password hash" "$ADGUARD_HASH"

    # Plaintext password + CF DNS-edit token from Docker .env
    ADGUARD_DOCKER_ENV=$(ssh_cmd "$ADGUARD_HOST" "sudo cat /opt/docker/.env 2>/dev/null") || true
    _adguard_pw=$(echo "$ADGUARD_DOCKER_ENV" | grep -E '^ADGUARD_PASSWORD=' | head -1 | cut -d= -f2- | tr -d '"')
    ADGUARD_PASSWORD=$(or_existing "adguard_admin_password" "$_adguard_pw")
    _cf_dns_edit=$(echo "$ADGUARD_DOCKER_ENV" | grep -E '^CLOUDFLARE_DNS_EDIT_API_KEY=' | head -1 | cut -d= -f2- | tr -d '"')
    CF_DNS_EDIT_TOKEN=$(or_existing "cf_dns_edit_api_token" "$_cf_dns_edit")
    log_result "AdGuard plaintext password" "$ADGUARD_PASSWORD"
    log_result "Cloudflare DNS-edit token" "$CF_DNS_EDIT_TOKEN"

    # Certbot email from renewal config
    _certbot_email=$(ssh_cmd "$ADGUARD_HOST" "sudo grep -r email /etc/letsencrypt/renewal/ 2>/dev/null" | head -1 | sed 's/.*=\s*//' | tr -d ' ')
    CERTBOT_EMAIL=$(or_existing "certbot_email" "$_certbot_email")
    if [[ -z "$CERTBOT_EMAIL" || "$CERTBOT_EMAIL" == "CHANGEME" ]]; then
        CERTBOT_EMAIL=$(prompt_or_existing "certbot_email" "Certbot/Let's Encrypt email")
    fi
    log_result "Certbot email" "$CERTBOT_EMAIL"
fi

# =====================================================================
# Samba — user list (passwords can't be extracted, just names)
# =====================================================================
# Preserve existing samba passwords from the vault
declare -A SAMBA_EXISTING_PW=()
if [[ -n "${VAULT_CONTENT:-}" ]]; then
    # Parse the samba_users_passwords list from decrypted vault
    in_samba=false
    current_user=""
    while IFS= read -r line; do
        if [[ "$line" =~ ^samba_users_passwords: ]]; then
            in_samba=true
            continue
        fi
        if $in_samba; then
            # Stop at the next top-level key
            if [[ "$line" =~ ^[a-zA-Z_] ]]; then
                break
            fi
            if [[ "$line" =~ name:\ *\"?([^\"]+)\"? ]]; then
                current_user="${BASH_REMATCH[1]}"
            fi
            if [[ "$line" =~ password:\ *\"?([^\"]+)\"? && -n "$current_user" ]]; then
                pw="${BASH_REMATCH[1]}"
                if [[ "$pw" != "CHANGEME" ]]; then
                    SAMBA_EXISTING_PW["$current_user"]="$pw"
                fi
                current_user=""
            fi
        fi
    done <<< "$VAULT_CONTENT"
fi

info "Pulling Samba user list from Fileserver ($FILESERVER_HOST)..."

SAMBA_USERS=$(ssh_cmd "$FILESERVER_HOST" "pdbedit -L 2>/dev/null" | cut -d: -f1) || true
if [[ -n "$SAMBA_USERS" ]]; then
    info "  Samba users found: $(echo "$SAMBA_USERS" | tr '\n' ', ')"
    if [[ ${#SAMBA_EXISTING_PW[@]} -gt 0 ]]; then
        info "  Samba passwords preserved from vault for: ${!SAMBA_EXISTING_PW[*]}"
    fi
else
    warn "  Samba: pdbedit not available or no users"
fi

# =====================================================================
# Write vault.yml
# =====================================================================
echo ""
info "Writing vault file to $VAULT_FILE ..."

# Helper: write a YAML key-value pair with proper quoting
# Most values are safe in double quotes. Single quotes for $ and { chars.
yml() {
    local key="$1"
    local val
    val=$(safe "${2:-}")
    # Use single quotes if value contains $, {, or }
    if [[ "$val" == *'$'* || "$val" == *'{'* || "$val" == *'}'* ]]; then
        printf '%s: '\''%s'\''\n' "$key" "$val"
    else
        printf '%s: "%s"\n' "$key" "$val"
    fi
}

VAULT_TMP=$(mktemp)
{
cat << 'HEADER'
# Auto-generated by pull-secrets.sh — encrypted automatically on write.
# Edit with: ansible-vault edit inventory/group_vars/all/vault.yml
# Run playbooks with: --ask-vault-pass  or  --vault-password-file
HEADER
echo ""
echo "# --- Cloudflare ---"
yml cf_api_token "$CF_API_TOKEN"
yml cf_account_id "$CF_ACCOUNT_ID"
yml cf_tunnel_token "$CF_TUNNEL_TOKEN"
yml cf_ddns_api_token "$CF_DDNS_TOKEN"
echo ""
echo "# --- Proxmox ---"
yml pve_api_token_id "$PVE_TOKEN_ID"
yml pve_api_token_secret "$PVE_TOKEN_SECRET"
echo ""
echo "# --- Authelia ---"
yml authelia_jwt_secret "$AUTHELIA_JWT"
yml authelia_session_secret "$AUTHELIA_SESSION"
yml authelia_storage_encryption_key "$AUTHELIA_STORAGE_ENC"
echo ""
echo "# --- Traefik / ACME ---"
yml traefik_cf_dns_api_token "$TRAEFIK_CF_TOKEN"
echo ""
echo "# --- Homarr ---"
yml homarr_secret_key "$HOMARR_SECRET"
echo ""
echo "# --- Mealie ---"
yml mealie_postgres_user "$MEALIE_PG_USER"
yml mealie_postgres_password "$MEALIE_PG_PASS"
echo ""
echo "# --- Monitoring ---"
yml grafana_admin_password "$GRAFANA_PW"
yml alertmanager_webhook_url "$ALERTMANAGER_WEBHOOK"
yml unpoller_unifi_user "$UNPOLLER_USER"
yml unpoller_unifi_pass "$UNPOLLER_PASS"
echo ""
echo "# --- Arr Stack ---"
yml sonarr_api_key "$SONARR_API_KEY"
yml radarr_api_key "$RADARR_API_KEY"
yml lidarr_api_key "$LIDARR_API_KEY"
yml prowlarr_api_key "$PROWLARR_API_KEY"
yml bazarr_api_key "$BAZARR_API_KEY"
yml jellyseerr_api_key "$JELLYSEERR_API_KEY"
yml recyclarr_sonarr_api_key "$RECYCLARR_SONARR_KEY"
yml recyclarr_radarr_api_key "$RECYCLARR_RADARR_KEY"
echo ""
echo "# --- VPN (seedbox) ---"
yml protonvpn_private_key "$PROTONVPN_KEY"
yml gluetun_control_server_key "$GLUETUN_KEY"
echo ""
echo "# --- MusicBrainz ---"
yml musicbrainz_postgres_password "$MUSICBRAINZ_PG_PASS"
echo ""
echo "# --- Home Assistant ---"
yml homeassistant_bearer_token "$HA_BEARER_TOKEN"
echo ""
echo "# --- Samba ---"
echo "samba_users_passwords:"
if [[ -n "$SAMBA_USERS" ]]; then
    while IFS= read -r user; do
        pw="${SAMBA_EXISTING_PW[$user]:-CHANGEME}"
        echo "  - name: \"$user\""
        echo "    password: \"$pw\""
    done <<< "$SAMBA_USERS"
else
    echo '  - name: "media"'
    echo "    password: \"${SAMBA_EXISTING_PW[media]:-CHANGEME}\""
    echo '  - name: "mediauser"'
    echo "    password: \"${SAMBA_EXISTING_PW[mediauser]:-CHANGEME}\""
fi
echo ""
echo "# --- rclone (PBS) ---"
yml rclone_gdrive_client_id "$RCLONE_CLIENT_ID"
yml rclone_gdrive_client_secret "$RCLONE_CLIENT_SECRET"
# Token is JSON with braces, force single quotes
printf 'rclone_gdrive_token: '\''%s'\''\n' "$(safe "$RCLONE_TOKEN")"
echo ""
echo "# --- AdGuard Home ---"
# bcrypt hash contains $, force single quotes
printf 'adguard_admin_password_hash: '\''%s'\''\n' "$(safe "$ADGUARD_HASH")"
yml adguard_admin_password "$ADGUARD_PASSWORD"
yml cf_dns_edit_api_token "$CF_DNS_EDIT_TOKEN"
yml certbot_email "$CERTBOT_EMAIL"
echo ""
} > "$VAULT_TMP"

mv "$VAULT_TMP" "$VAULT_FILE"

echo ""
info "Done! Vault file written to: $VAULT_FILE"
echo ""

# Count how many are still CHANGEME — must check now, before encrypting,
# since grep can't read the ciphertext afterwards.
REMAINING=$(grep -c 'CHANGEME' "$VAULT_FILE" || true)
if [[ "$REMAINING" -gt 0 ]]; then
    warn "$REMAINING values still set to CHANGEME:"
    grep -n 'CHANGEME' "$VAULT_FILE" | sed 's/^/  /'
fi

# Guard: this file holds live secrets in cleartext. Encrypt it immediately so
# a stray `git add` can never commit plaintext. ansible-vault uses
# ANSIBLE_VAULT_PASSWORD_FILE if set, otherwise it prompts.
echo ""
info "Encrypting $VAULT_FILE with ansible-vault ..."
if [[ -z "${ANSIBLE_VAULT_PASSWORD_FILE:-}" ]]; then
    warn "  tip: set ANSIBLE_VAULT_PASSWORD_FILE to avoid being prompted here"
    warn "  (and make sure it matches your existing vault password — a"
    warn "   mismatched password will re-key the vault and break playbooks)"
fi
if ansible-vault encrypt "$VAULT_FILE"; then
    info "Vault file encrypted."
else
    fail "ansible-vault encrypt FAILED — $VAULT_FILE is PLAINTEXT on disk."
    fail "Do NOT commit it. Encrypt it manually before anything else:"
    fail "  ansible-vault encrypt $VAULT_FILE"
    exit 1
fi

echo ""
echo "Next steps:"
if [[ "$REMAINING" -gt 0 ]]; then
    echo "  1. Fill in the $REMAINING CHANGEME value(s): ansible-vault edit $VAULT_FILE"
    echo "  2. The file is encrypted and safe to commit."
else
    echo "  The file is encrypted and safe to commit."
fi
echo ""
