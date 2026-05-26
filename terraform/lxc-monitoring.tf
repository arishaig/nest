# ──────────────────────────────────────────────
# LXC 105: monitoring — Prometheus/Grafana/Loki
# ──────────────────────────────────────────────

resource "proxmox_virtual_environment_container" "monitoring" {
  node_name   = var.pve_node
  vm_id       = 105
  description = "Monitoring stack (Prometheus, Grafana, Loki)"
  tags        = ["docker", "monitoring"]

  unprivileged  = true
  start_on_boot = true

  operating_system {
    template_file_id = proxmox_download_file.debian12_ct.id
    type             = "debian"
  }

  cpu {
    cores = 2
  }

  memory {
    dedicated = 12288
    swap      = 12288
  }

  disk {
    datastore_id = "Tank"
    size         = 50
  }

  network_interface {
    name     = "eth0"
    bridge   = "vmbr0"
    firewall = true
  }

  initialization {
    hostname = "monitoring"
    ip_config {
      ipv4 {
        address = "192.168.1.44/24"
        gateway = var.gateway
      }
    }
    dns {
      servers = var.dns_servers
    }
  }

  features {
    nesting = true
  }

  startup {
    order = 2
  }

  provisioner "local-exec" {
    command = "ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/common.yml ../playbooks/provision/monitoring.yml --vault-password-file ~/.config/ansible-on-nest/vault-pass --limit monitoring"
  }

  lifecycle {
    ignore_changes = [
      operating_system,
      console,
    ]
  }
}
