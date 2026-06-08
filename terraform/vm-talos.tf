# ──────────────────────────────────────────────
# VM 110: Talos Linux — single-node Kubernetes
# ──────────────────────────────────────────────
# Single control-plane node that also schedules workloads
# (allowSchedulingOnControlPlanes: true in talos/patches/controlplane.yaml).
# Bootstrap: run scripts/bootstrap-talos.sh after `terraform apply`.

resource "proxmox_download_file" "talos_iso" {
  node_name    = var.pve_node
  content_type = "iso"
  datastore_id = "local"
  url          = "https://factory.talos.dev/image/${var.talos_schematic_id}/${var.talos_version}/metal-amd64.iso"
  file_name    = "talos-${var.talos_version}-metal-amd64.iso"
}

resource "proxmox_virtual_environment_vm" "talos" {
  node_name   = var.pve_node
  vm_id       = 110
  name        = "talos"
  description = "Talos Linux — single-node Kubernetes cluster"
  tags        = ["kubernetes", "infrastructure"]

  on_boot = true
  started = true

  bios = "ovmf"

  agent {
    enabled = true
    timeout = "30s"
  }

  cpu {
    cores = 4
    type  = "host"
  }

  memory {
    dedicated = 8192
  }

  efi_disk {
    datastore_id = "local-zfs"
    type         = "4m"
  }

  disk {
    datastore_id = "local-zfs"
    interface    = "scsi0"
    size         = 100
    iothread     = true
    discard      = "on"
    ssd          = true
  }

  scsi_hardware = "virtio-scsi-single"

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
