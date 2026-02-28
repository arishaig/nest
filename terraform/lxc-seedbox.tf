# ──────────────────────────────────────────────
# LXC 104: seedbox — Torrent client with VPN
# ──────────────────────────────────────────────
# Requires TUN device passthrough for VPN (WireGuard/OpenVPN).
# The lxc.* options must be added to /etc/pve/lxc/104.conf after
# Terraform creates the container.

resource "proxmox_virtual_environment_container" "seedbox" {
  node_name   = var.pve_node
  vm_id       = 104
  description = "Seedbox with VPN"
  tags        = ["docker", "media"]

  unprivileged  = true
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
    firewall = true
  }

  initialization {
    hostname = "seedbox"
    ip_config {
      ipv4 {
        address = "dhcp"
      }
    }
    dns {
      servers = [var.dns_server]
    }
  }

  features {
    nesting = true
  }

  mount_point {
    volume = "/Tank/media_root"
    path   = "/mnt/data"
  }

  startup {
    order = 3
  }

  provisioner "local-exec" {
    command = "ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/seedbox.yml --limit seedbox"
  }

  lifecycle {
    ignore_changes = [
      operating_system,
      description,
      console,
    ]
  }
}

# Post-creation manual step for TUN device:
# Add to /etc/pve/lxc/104.conf:
#   lxc.cgroup2.devices.allow: c 10:200 rwm
#   lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
