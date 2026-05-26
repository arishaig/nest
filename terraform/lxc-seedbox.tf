# ──────────────────────────────────────────────
# LXC 104: seedbox — Torrent client with VPN
# ──────────────────────────────────────────────
# Requires TUN device passthrough for VPN (WireGuard/OpenVPN).
# Passthrough is automated via null_resource.seedbox_passthrough
# which runs pve-passthrough.yml on the PVE host.

resource "proxmox_virtual_environment_container" "seedbox" {
  node_name   = var.pve_node
  vm_id       = 104
  description = "Seedbox with VPN"
  tags        = ["docker", "media"]

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
        address = "192.168.1.182/24"
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

  mount_point {
    volume = "/Tank/media_root"
    path   = "/mnt/data"
  }

  startup {
    order = 3
  }

  lifecycle {
    ignore_changes = [
      operating_system,
      console,
    ]
  }
}

resource "null_resource" "seedbox_passthrough" {
  depends_on = [proxmox_virtual_environment_container.seedbox]

  triggers = {
    container_id = proxmox_virtual_environment_container.seedbox.id
  }

  provisioner "local-exec" {
    command = "ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/pve-passthrough.yml --vault-password-file ~/.config/ansible-on-nest/vault-pass -e lxc_id=104 -e @../playbooks/provision/files/seedbox/passthrough.yml"
  }
}

resource "null_resource" "seedbox_provision" {
  depends_on = [null_resource.seedbox_passthrough]

  triggers = {
    container_id = proxmox_virtual_environment_container.seedbox.id
  }

  provisioner "local-exec" {
    command = "ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/common.yml ../playbooks/provision/seedbox.yml --vault-password-file ~/.config/ansible-on-nest/vault-pass --limit seedbox"
  }
}
