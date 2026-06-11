# ──────────────────────────────────────────────
# VM 500: Proxmox Backup Server
# ──────────────────────────────────────────────

resource "proxmox_virtual_environment_vm" "backup" {
  node_name   = var.pve_node
  vm_id       = 500
  name        = "backup"
  description = "Proxmox Backup Server"
  tags        = ["infrastructure"]

  on_boot = true
  started = true

  agent {
    enabled = true
    timeout = "30s"
  }

  cpu {
    cores = 4
    type  = "x86-64-v2-AES"
  }

  # 16 GB is generous for PBS at current datastore sizes. The host OOM-killed
  # this VM at 33 GB RSS (2026-06-11) when total VM allocation exceeded host RAM.
  memory {
    dedicated = 16384
  }

  disk {
    datastore_id = "local-zfs"
    interface    = "scsi0"
    size         = 500
    iothread     = true
  }

  scsi_hardware = "virtio-scsi-single"

  cdrom {
    file_id   = "local:iso/proxmox-backup-server_3.4-1.iso"
    interface = "ide2"
  }

  network_device {
    bridge   = "vmbr0"
    firewall = true
  }

  operating_system {
    type = "l26"
  }

  lifecycle {
    ignore_changes = [
      disk,
      cdrom,
    ]
  }
}
