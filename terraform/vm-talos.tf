# ──────────────────────────────────────────────
# VM 110: Talos Linux — single-node Kubernetes
# ──────────────────────────────────────────────
# Single control-plane node that also schedules workloads
# (allowSchedulingOnControlPlanes: true in talos/patches/controlplane.yaml).
# Bootstrap: run scripts/bootstrap-talos.sh after `tofu apply`.

resource "proxmox_download_file" "talos_iso" {
  node_name           = var.pve_node
  content_type        = "iso"
  datastore_id        = "local"
  url                 = "https://factory.talos.dev/image/${var.talos_schematic_id}/${var.talos_version}/metal-amd64.iso"
  file_name           = "talos-${var.talos_version}-metal-amd64.iso"
  overwrite           = true
  overwrite_unmanaged = true
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
    enabled = false
  }

  cpu {
    cores = 4
    type  = "host"
  }

  memory {
    # 30GB: monitoring LXC reduced 12GB→6GB 2026-06-25, freeing 6GB for alpha.
    # (Was 32GB; reduced to 24GB on 2026-06-14 after host OOM when Anagnorisis
    # loaded CLAP+Jina models. Raised back to 30GB after alpha kubelet OOM kills.)
    dedicated = 30720
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

  boot_order = ["scsi0", "ide0"]

  cdrom {
    file_id   = ""
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
      efi_disk,
    ]
  }
}
