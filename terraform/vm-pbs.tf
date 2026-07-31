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

  # scsi0 holds the pbs-local datastore. Note it lives on local-zfs (= rpool),
  # the same pool as the guests it backs up — see architecture review finding
  # B2. That is mitigated, not fixed, by a second datastore on Tank:
  #
  #   scsi1: Tank:vm-500-disk-0, 1000G -> PBS datastore "tank-archive"
  #
  # scsi1 is deliberately absent from this resource. lifecycle.ignore_changes
  # below covers `disk`, so a second disk block here would be silently ignored,
  # and dropping `disk` from that list would make Terraform try to reconcile
  # scsi0 as well. It was therefore attached out of band, the same way LXC
  # disks use `pct set`:
  #
  #   qm set 500 --scsi1 Tank:1000,iothread=1
  #
  # playbooks/provision/pbs.yml formats it and registers the datastore.
  #
  # scsi0 also carries a write throttle (mbps_wr=150, iops_wr=3000), applied
  # out of band the same way — `disk` is ignore_changes'd, so it wouldn't
  # survive a `tf apply` edit here regardless:
  #
  #   qm set 500 --scsi0 local-zfs:vm-500-disk-0,iothread=1,size=500G,mbps_wr=150,iops_wr=3000
  #
  # Added 2026-07-31: backup's bursty writes on local-zfs (shared NVMe mirror
  # with talos-alpha-control) were stalling etcd fsyncs on alpha-control long
  # enough to blow leader-election timeouts, crash-looping kube-scheduler and
  # kube-controller-manager. This caps backup's ceiling so etcd's small
  # latency-critical writes aren't queued behind it. Re-apply if this disk is
  # ever recreated.
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
