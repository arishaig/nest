# ──────────────────────────────────────────────
# PVE Storage Definitions
# ──────────────────────────────────────────────
# Note: The underlying ZFS pools (rpool, Tank) are created at OS
# install time. Terraform only manages PVE's storage references.

# local (dir) — ships with PVE, stores ISOs and templates
# Managed by PVE itself; importing would conflict. Left as documentation.

# local-zfs — rpool/data for VM/CT disks
# This is auto-created by PVE installer. Left as documentation.

# Tank — ZFS pool for bulk storage
# This is created via `zpool create` at OS level.
# The PVE storage definition references it:
#
# resource "proxmox_virtual_environment_storage" "tank" {
#   storage_id = "Tank"
#   type       = "zfspool"
#   pool       = "Tank"
#   content    = ["images", "rootdir"]
#   nodes      = ["pve"]
# }
#
# Note: The bpg/proxmox-ve provider has limited storage resource
# support. Storage definitions may need to be managed via the PVE
# API directly or left as-is after initial setup.

# PBS — Proxmox Backup Server
# The PBS storage connection is configured in PVE's storage.cfg.
# The provider does not yet support proxmox_virtual_environment_storage_pbs
# as a first-class resource. This is documented for reference:
#
# PBS config in /etc/pve/storage.cfg:
#   pbs: backup
#     datastore pbs-local
#     server 192.168.1.113
#     port 8007
#     content backup
#     fingerprint <pbs_fingerprint>
#     prune-backups keep-all=1
#     username root@pam

# cold-storage and cold-backup are directory-type storage for
# external/cold backup drives. These are also configured in storage.cfg.
