# ──────────────────────────────────────────────
# VM 112: talos-alpha-control — dedicated control-plane node
# ──────────────────────────────────────────────
# Non-schedulable control-plane-only node (allowSchedulingOnControlPlanes:
# false in talos/patches/controlplane-alpha-control.yaml). Joins the existing
# alpha+beta-rpi5 etcd as a third, fault-tolerant member — see
# /home/isaac/.claude/plans/silly-riding-stroustrup.md for the full migration
# this is Phase 1 of. Once alpha and beta/gamma are converted to workers,
# this becomes the cluster's sole control-plane node.
#
# Bootstrap: after `tofu apply`, join via
#   ./scripts/join-talos-node.sh alpha-control <dhcp-ip>

resource "proxmox_virtual_environment_vm" "talos_alpha_control" {
  node_name   = var.pve_node
  vm_id       = 112
  name        = "talos-alpha-control"
  description = "Talos Linux — dedicated control-plane node"
  tags        = ["kubernetes", "infrastructure", "control-plane"]

  on_boot = true
  started = true

  bios = "ovmf"

  agent {
    enabled = true
  }

  cpu {
    # Bumped from 2 (2026-08-28): 2 vCPU is under-provisioned for a sole
    # control-plane node. During the 2026-08-28 alert storm this VM sat at
    # load ~3 (1.5x per core); the apiserver went latency-bound and every
    # Flux controller lost its leader-election lease and crash-looped,
    # kube-state-metrics failed its liveness probe, and ~11 downstream
    # KubePodCrashLooping alerts flapped resolve/re-fire all evening. Host
    # has 12 cores; talos-alpha (worker) uses 8, everything else is light.
    cores = 4
    type  = "host"
  }

  memory {
    # 8192 bumped from 4096 (2026-08-03): 4GB is thin headroom for a sole
    # control-plane node (kube-apiserver alone routinely uses 800MB+).
    # 12288 (2026-08-28): further headroom taken alongside the 2->4 vCPU
    # bump during the apiserver-latency incident. The proximate cause looked
    # CPU/etcd-fsync bound rather than RSS-bound, but a control plane this
    # small has no slack to diagnose under. Host had ~22GB free at the time.
    dedicated = 12288
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

  # Needed for first boot only (installs to scsi0, then boots straight from
  # disk). Once the node has joined and installed, follow the pattern from
  # #242/6ba619c: set file_id = "" and drop `cdrom` from ignore_changes,
  # since PVE re-validates the cdrom attachment on every start and the ISO
  # is not kept around in local storage long-term.
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
