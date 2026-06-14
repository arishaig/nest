# ──────────────────────────────────────────────
# LXC 100: docker — Main Docker host
# ──────────────────────────────────────────────

resource "proxmox_download_file" "debian_ct" {
  node_name    = var.pve_node
  content_type = "vztmpl"
  datastore_id = "local"
  # Browse new templates: http://download.proxmox.com/images/system/
  url                 = "http://download.proxmox.com/images/system/debian-13-standard_13.1-2_amd64.tar.zst"
  overwrite           = false
  overwrite_unmanaged = false
}

resource "proxmox_virtual_environment_container" "docker" {
  node_name   = var.pve_node
  vm_id       = 100
  description = "Main Docker host"
  tags        = ["docker", "infrastructure"]

  unprivileged  = true
  start_on_boot = false
  # Decommissioned: keep the LXC stopped. Without this, the provider default
  started = true

  operating_system {
    template_file_id = proxmox_download_file.debian_ct.id
    type             = "debian"
  }

  cpu {
    cores = 10
  }

  memory {
    dedicated = 12288
    swap      = 512
  }

  disk {
    datastore_id = "local-zfs"
    size         = 64
  }

  network_interface {
    name     = "eth0"
    bridge   = "vmbr0"
    firewall = false
  }

  initialization {
    hostname = "docker"
    ip_config {
      ipv4 {
        address = "192.168.1.158/24"
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

  mount_point {
    volume = "/rpool/data/docker-apps"
    path   = "/mnt/app_config"
  }

  mount_point {
    volume = "/Tank/personal"
    path   = "/mnt/personal"
  }

  startup {
    order    = 1
    up_delay = 15
  }

  provisioner "local-exec" {
    command = "ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/common.yml ../playbooks/provision/docker-host.yml --vault-password-file ~/.config/ansible-on-nest/vault-pass --skip-tags wireguard --limit docker"
  }

  lifecycle {
    ignore_changes = [
      operating_system,
      console,
      initialization,
    ]
  }
}
