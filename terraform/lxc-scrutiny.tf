# ──────────────────────────────────────────────
# LXC 103: scrutiny — Disk health monitoring
# ──────────────────────────────────────────────
# This container requires device passthrough for SMART data.
# The lxc.* options below must be set manually in /etc/pve/lxc/103.conf
# after Terraform creates the container, as the provider does not
# support all LXC raw config options.

resource "proxmox_virtual_environment_container" "scrutiny" {
  node_name   = var.pve_node
  vm_id       = 103
  description = "Scrutiny disk health monitoring"
  tags        = ["docker", "monitoring"]

  unprivileged  = false  # Needs device access
  start_on_boot = true

  operating_system {
    template_file_id = proxmox_virtual_environment_download_file.debian12_ct.id
    type             = "debian"
  }

  cpu {
    cores = 2
  }

  memory {
    dedicated = 2048
    swap      = 512
  }

  disk {
    datastore_id = "local-zfs"
    size         = 8
  }

  network_interface {
    name     = "eth0"
    bridge   = "vmbr0"
    firewall = false
  }

  initialization {
    hostname = "scrutiny"
    ip_config {
      ipv4 {
        address = "dhcp"
      }
    }
    dns {
      servers = [var.dns_server]
    }
  }

  startup {
    order = 3
  }

  provisioner "local-exec" {
    command = "ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/scrutiny.yml --limit scrutiny"
  }

  lifecycle {
    ignore_changes = [
      operating_system,
      description,
      console,
    ]
  }
}

# Post-creation manual step for device passthrough:
# Add to /etc/pve/lxc/103.conf:
#   lxc.cap.drop:
#   lxc.apparmor.profile: unconfined
#   lxc.cgroup2.devices.allow: b 8:* rwm
#   lxc.cgroup2.devices.allow: b 259:* rwm
#   lxc.cgroup2.devices.allow: c 21:* rwm
#   lxc.mount.entry: /dev/sda dev/sda none bind,optional,create=file
#   lxc.mount.entry: /dev/sdb dev/sdb none bind,optional,create=file
#   lxc.mount.entry: /dev/sdc dev/sdc none bind,optional,create=file
#   lxc.mount.entry: /dev/sdd dev/sdd none bind,optional,create=file
#   lxc.mount.entry: /dev/sde dev/sde none bind,optional,create=file
#   lxc.mount.entry: /dev/sdf dev/sdf none bind,optional,create=file
#   lxc.mount.entry: /dev/sdg dev/sdg none bind,optional,create=file
#   lxc.mount.entry: /dev/sdh dev/sdh none bind,optional,create=file
#   lxc.mount.entry: /dev/nvme0n1 dev/nvme0n1 none bind,optional,create=file
#   lxc.mount.entry: /dev/nvme1n1 dev/nvme1n1 none bind,optional,create=file
#   lxc.mount.entry: /dev/sg0 dev/sg0 none bind,optional,create=file
#   lxc.mount.entry: /dev/sg1 dev/sg1 none bind,optional,create=file
#   lxc.mount.entry: /dev/sg2 dev/sg2 none bind,optional,create=file
#   lxc.mount.entry: /dev/sg3 dev/sg3 none bind,optional,create=file
#   lxc.mount.entry: /dev/sg4 dev/sg4 none bind,optional,create=file
#   lxc.mount.entry: /dev/sg5 dev/sg5 none bind,optional,create=file
#   lxc.mount.entry: /dev/sg6 dev/sg6 none bind,optional,create=file
#   lxc.mount.entry: /dev/sg7 dev/sg7 none bind,optional,create=file
