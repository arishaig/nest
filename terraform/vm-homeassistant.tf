# ──────────────────────────────────────────────
# VM 107: Home Assistant OS
# ──────────────────────────────────────────────
# HAOS is self-contained — no Ansible provisioner needed.
# Restore from Proxmox Backup Server backup after creation.

resource "proxmox_virtual_environment_vm" "homeassistant" {
  node_name   = var.pve_node
  vm_id       = 107
  name        = "homeassistant"
  description = <<-EOT
    <div align='center'><a href='https://Helper-Scripts.com' target='_blank' rel='noopener noreferrer'><img src='https://raw.githubusercontent.com/tteck/Proxmox/main/misc/images/logo-81x112.png'/></a>

    # Home Assistant OS

    <a href='https://ko-fi.com/D1D7EP4GF'><img src='https://img.shields.io/badge/&#x2615;-Buy me a coffee-blue' /></a>
    </div>
  EOT
  tags        = ["proxmox-helper-scripts"]
  pool_id     = "IoT"

  on_boot = true
  started = true

  bios    = "ovmf"

  agent {
    enabled = true
  }

  cpu {
    cores = 2
    type  = "host"
  }

  memory {
    dedicated = 16384
  }

  efi_disk {
    datastore_id = "local-zfs"
    type         = "4m"
  }

  disk {
    datastore_id = "local-zfs"
    interface    = "scsi0"
    size         = 32
    cache        = "writethrough"
    discard      = "on"
    ssd          = true
  }

  scsi_hardware = "virtio-scsi-pci"

  network_device {
    bridge  = "vmbr0"
    vlan_id = 4
  }

  operating_system {
    type = "l26"
  }

  tablet_device = false

  # USB passthrough for Zigbee/Z-Wave controllers
  usb {
    host = "1-5"
  }
  usb {
    host = "3-4"
  }
  usb {
    host = "0bda:a728"
  }

  lifecycle {
    ignore_changes = [
      disk,
      efi_disk,
      description,
      network_device,
    ]
  }
}
