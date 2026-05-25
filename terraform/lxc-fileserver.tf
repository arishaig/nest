# ──────────────────────────────────────────────
# LXC 102: fileserver — Samba file server
# ──────────────────────────────────────────────

resource "proxmox_virtual_environment_container" "fileserver" {
  node_name   = var.pve_node
  vm_id       = 102
  description = "Samba file server"
  tags        = ["fileserver"]

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
    dedicated = 1024
    swap      = 1024
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
    hostname = "fileserver"
    ip_config {
      ipv4 {
        address = "192.168.1.17/24"
        gateway = var.gateway
      }
    }
    dns {
      servers = var.dns_servers
    }
  }

  features {
    nesting = true
    keyctl  = true
  }

  mount_point {
    volume = "/Tank/media_root"
    path   = "/mnt/media_root"
  }

  startup {
    order = 2
  }

  provisioner "local-exec" {
    command = "ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/common.yml ../playbooks/provision/fileserver.yml --limit fileserver"
  }

  lifecycle {
    ignore_changes = [
      operating_system,
      console,
    ]
  }
}
