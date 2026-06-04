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
    template_file_id = proxmox_download_file.debian_ct.id
    type             = "debian"
  }

  cpu {
    cores = 1
  }

  memory {
    dedicated = 2048
    swap      = 512
  }

  disk {
    datastore_id = "local-zfs"
    size         = 4
  }

  network_interface {
    name     = "eth0"
    bridge   = "vmbr0"
    vlan_id  = 5
    firewall = true
  }

  initialization {
    hostname = "dns-secondary"
    ip_config {
      ipv4 {
        address = "192.168.7.8/24"
        gateway = "192.168.7.1"
      }
    }
    dns {
      servers = var.dns_servers
    }
    user_account {
      keys = var.ssh_public_keys
    }
  }

  features {
    nesting = true
  }

  startup {
    order = 1
  }

  provisioner "local-exec" {
    command = "ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/common.yml ../playbooks/provision/adguard.yml --vault-password-file ~/.config/ansible-on-nest/vault-pass --limit dns-secondary"
  }

  lifecycle {
    ignore_changes = [
      operating_system,
      console,
      initialization,
    ]
  }
}
