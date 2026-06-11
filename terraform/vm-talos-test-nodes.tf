# ──────────────────────────────────────────────────────────────────────────────
# VMs 111–112: Talos test control-plane nodes
#
# Temporary x86 VMs used to validate multi-node k8s behavior (etcd HA, VIP,
# MetalLB, Traefik LoadBalancer) before RPi5 hardware arrives.
# Remove this file and run `terraform apply` once RPi5s replace them.
# ──────────────────────────────────────────────────────────────────────────────

resource "proxmox_virtual_environment_vm" "talos_beta_vm" {
  node_name   = var.pve_node
  vm_id       = 111
  name        = "talos-beta-vm"
  description = "Talos Linux — test control-plane node (temporary, replace with RPi5)"
  tags        = ["kubernetes", "infrastructure", "test"]

  on_boot = true
  started = true

  bios = "ovmf"

  agent {
    enabled = true
    timeout = "30s"
  }

  cpu {
    cores = 2
    type  = "host"
  }

  memory {
    dedicated = 4096
  }

  efi_disk {
    datastore_id = "local-zfs"
    type         = "4m"
  }

  disk {
    datastore_id = "local-zfs"
    interface    = "scsi0"
    size         = 40
    iothread     = true
    discard      = "on"
    ssd          = true
  }

  scsi_hardware = "virtio-scsi-single"

  boot_order = ["scsi0", "ide0"]

  cdrom {
    file_id   = proxmox_download_file.talos_iso.id
    interface = "ide0"
  }

  network_device {
    bridge = "vmbr0"
  }

  operating_system {
    type = "l26"
  }

  tablet_device = false

  lifecycle {
    ignore_changes = [
      disk,
      cdrom,
      efi_disk,
    ]
  }
}

resource "proxmox_virtual_environment_vm" "talos_gamma_vm" {
  node_name   = var.pve_node
  vm_id       = 112
  name        = "talos-gamma-vm"
  description = "Talos Linux — test control-plane node (temporary, replace with RPi5)"
  tags        = ["kubernetes", "infrastructure", "test"]

  on_boot = true
  started = true

  bios = "ovmf"

  agent {
    enabled = true
    timeout = "30s"
  }

  cpu {
    cores = 2
    type  = "host"
  }

  memory {
    dedicated = 4096
  }

  efi_disk {
    datastore_id = "local-zfs"
    type         = "4m"
  }

  disk {
    datastore_id = "local-zfs"
    interface    = "scsi0"
    size         = 40
    iothread     = true
    discard      = "on"
    ssd          = true
  }

  scsi_hardware = "virtio-scsi-single"

  boot_order = ["scsi0", "ide0"]

  cdrom {
    file_id   = proxmox_download_file.talos_iso.id
    interface = "ide0"
  }

  network_device {
    bridge = "vmbr0"
  }

  operating_system {
    type = "l26"
  }

  tablet_device = false

  lifecycle {
    ignore_changes = [
      disk,
      cdrom,
      efi_disk,
    ]
  }
}
