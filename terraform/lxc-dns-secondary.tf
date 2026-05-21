# ──────────────────────────────────────────────
# LXC 106: dns-secondary — AdGuard Home + Unbound
# ──────────────────────────────────────────────

resource "proxmox_virtual_environment_container" "dns_secondary" {
  node_name   = var.pve_node
  vm_id       = 106
  description = "Secondary AdGuard Home + Unbound (DNS failover)"
  tags        = ["dns", "infrastructure"]

  unprivileged  = true
  start_on_boot = true

  operating_system {
    template_file_id = proxmox_virtual_environment_download_file.debian12_ct.id
    type             = "debian"
  }

  cpu {
    cores = 1
  }

  memory {
    dedicated = 512
    swap      = 512
  }

  disk {
    datastore_id = "local-zfs"
    size         = 4
  }

  network_interface {
    name     = "eth0"
    bridge   = "vmbr0"
    firewall = true
  }

  initialization {
    hostname = "dns-secondary"
    ip_config {
      ipv4 {
        address = "192.168.1.53/24"
        gateway = var.gateway
      }
    }
    dns {
      servers = [var.dns_server]
    }
    user_account {
      keys = var.ssh_public_keys
    }
  }

  startup {
    order = 1
  }

  provisioner "local-exec" {
    command = "ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/adguard.yml --limit dns-secondary"
  }

  lifecycle {
    ignore_changes = [
      operating_system,
      console,
    ]
  }
}
