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
    enabled = true
  }

  cpu {
    # 8 cores: at 4, evening media load (Jellyfin + transcode jobs) pinned the
    # VM at ~100% for 15+ min stretches (2026-07-08 03:36Z incident) — starved
    # apiserver/etcd, cancelled controller watch streams, and crash-looped
    # MetalLB via liveness timeouts. CPU requests were also 97% committed,
    # blocking scheduling. Host has 12 cores at load ~2.
    # NOTE: applying this reboots the VM (brief full-cluster outage).
    cores = 8
    type  = "host"
  }

  memory {
    # 40GB: beta/delta test VMs (4GB+8GB) removed 2026-06-26 — RAM reclaimed for
    # alpha once RPi5s made the multi-node rehearsal purpose redundant.
    # (History: was 32GB → 24GB 2026-06-14 after host OOM from Anagnorisis
    # CLAP+Jina models → 30GB 2026-06-25 after kubelet OOM kills.)
    dedicated = 40960
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
