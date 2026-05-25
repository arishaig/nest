# ──────────────────────────────────────────────
# LXC 103: scrutiny — Disk health monitoring
# ──────────────────────────────────────────────
# This container requires device passthrough for SMART data.
# Passthrough is automated via null_resource.scrutiny_passthrough
# which runs pve-passthrough.yml on the PVE host.

resource "proxmox_virtual_environment_container" "scrutiny" {
  node_name   = var.pve_node
  vm_id       = 103
  description = "Scrutiny disk health monitoring"
  tags        = ["docker", "monitoring"]

  unprivileged  = false # Needs device access
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
        address = "192.168.1.46/24"
        gateway = var.gateway
      }
    }
    dns {
      servers = var.dns_servers
    }
    user_account {
      keys = [
        trimspace(file("~/.ssh/id_ed25519.pub")),        # Personal key
        trimspace(file("~/.ssh/ansible-on-nest.pub")),   # Ansible key
      ]
    }
  }

  startup {
    order = 3
  }

  lifecycle {
    ignore_changes = [
      operating_system,
      console,
      initialization,
    ]
  }
}

resource "null_resource" "scrutiny_passthrough" {
  depends_on = [proxmox_virtual_environment_container.scrutiny]

  triggers = {
    container_id = proxmox_virtual_environment_container.scrutiny.id
  }

  provisioner "local-exec" {
    command = "ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/pve-passthrough.yml -e lxc_id=103 -e @../playbooks/provision/files/scrutiny/passthrough.yml"
  }
}

resource "null_resource" "scrutiny_provision" {
  depends_on = [null_resource.scrutiny_passthrough]

  triggers = {
    container_id = proxmox_virtual_environment_container.scrutiny.id
  }

  provisioner "local-exec" {
    command = "ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/common.yml ../playbooks/provision/scrutiny.yml --limit scrutiny"
  }
}
