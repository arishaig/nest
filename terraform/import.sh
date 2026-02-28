#!/usr/bin/env bash
set -uo pipefail

# Import existing infrastructure into Terraform state.
# Run from the terraform/ directory:
#   chmod +x import.sh && ./import.sh
#
# Safe to re-run — already-imported resources will print a warning and continue.

OPTS="-var-file=secrets.tfvars"

import() {
  echo "  Importing $1..."
  terraform import $OPTS "$1" "$2" 2>&1 || echo "  (skipped — may already be imported)"
}

echo "=== Importing PVE Users ==="
import proxmox_virtual_environment_user.prometheus prometheus@pve
import proxmox_virtual_environment_user.svc_homeassistant svc_homeassistant@pve

echo "=== Importing PVE Groups ==="
import proxmox_virtual_environment_group.homeassistant HomeAssistant

echo "=== Importing PVE ACLs ==="
import proxmox_virtual_environment_acl.prometheus_auditor '/?prometheus@pve?PVEAuditor'
import proxmox_virtual_environment_acl.homeassistant_auditor '/?HomeAssistant?PVEAuditor'

echo "=== Importing PVE API Tokens ==="
import proxmox_virtual_environment_user_token.homarr 'root@pam!Homarr'
import proxmox_virtual_environment_user_token.backup 'root@pam!backup'

# NOTE: proxmox_virtual_environment_download_file does not support import.
# Terraform will download the template on first apply if not present.

echo "=== Importing LXC Containers ==="
import proxmox_virtual_environment_container.docker proxmox/100
import proxmox_virtual_environment_container.musicbrainz proxmox/101
import proxmox_virtual_environment_container.fileserver proxmox/102
import proxmox_virtual_environment_container.scrutiny proxmox/103
import proxmox_virtual_environment_container.seedbox proxmox/104
import proxmox_virtual_environment_container.monitoring proxmox/105

echo "=== Importing VMs ==="
import proxmox_virtual_environment_vm.homeassistant proxmox/107
import proxmox_virtual_environment_vm.backup proxmox/500

echo "=== Importing AdGuard DNS Rewrites ==="
# Format: domain||answer
import 'adguard_rewrite.dns["docker.local.arishaig.site"]' 'docker.local.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["proxmox.local.arishaig.site"]' 'proxmox.local.arishaig.site||192.168.1.16'
import 'adguard_rewrite.dns["torrent.local.arishaig.site"]' 'torrent.local.arishaig.site||192.168.1.182'
import 'adguard_rewrite.dns["scrutiny.local.arishaig.site"]' 'scrutiny.local.arishaig.site||192.168.1.46'
import 'adguard_rewrite.dns["backup.local.arishaig.site"]' 'backup.local.arishaig.site||192.168.1.113'
import 'adguard_rewrite.dns["backlight.local.arishaig.site"]' 'backlight.local.arishaig.site||192.168.4.97'
import 'adguard_rewrite.dns["dns.arishaig.site"]' 'dns.arishaig.site||192.168.7.7'
import 'adguard_rewrite.dns["monitoring.arishaig.site"]' 'monitoring.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["jellyfin.arishaig.site"]' 'jellyfin.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["jellyseerr.arishaig.site"]' 'jellyseerr.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["sonarr.arishaig.site"]' 'sonarr.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["radarr.arishaig.site"]' 'radarr.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["bazarr.arishaig.site"]' 'bazarr.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["lidarr.arishaig.site"]' 'lidarr.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["prowlarr.arishaig.site"]' 'prowlarr.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["nzbd.arishaig.site"]' 'nzbd.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["calibre.arishaig.site"]' 'calibre.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["chaptarr.arishaig.site"]' 'chaptarr.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["dash.arishaig.site"]' 'dash.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["uptime.arishaig.site"]' 'uptime.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["glances.arishaig.site"]' 'glances.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["mealie.arishaig.site"]' 'mealie.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["copyparty.arishaig.site"]' 'copyparty.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["recommendarr.arishaig.site"]' 'recommendarr.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["watcharr.arishaig.site"]' 'watcharr.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["auth.arishaig.site"]' 'auth.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["torrent.arishaig.site"]' 'torrent.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["proxmox.arishaig.site"]' 'proxmox.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["scrutiny.arishaig.site"]' 'scrutiny.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["backup.arishaig.site"]' 'backup.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["docker.arishaig.site"]' 'docker.arishaig.site||192.168.1.158'
import 'adguard_rewrite.dns["vpn.arishaig.site"]' 'vpn.arishaig.site||192.168.1.1'

echo "=== Importing AdGuard Config ==="
import adguard_config.main 1

echo "=== Importing AdGuard Filter Lists ==="
import adguard_list_filter.hagezi_pro 1771729533
import adguard_list_filter.hagezi_gambling 1771729534
import adguard_list_filter.hagezi_threat_intel 1771729535

echo ""
echo "=== Import complete ==="
echo "Run 'terraform plan -var-file=secrets.tfvars' to check for drift."
