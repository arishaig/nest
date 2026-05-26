# ──────────────────────────────────────────────
# LXC 101: musicbrainz — MusicBrainz Mirror
# ──────────────────────────────────────────────

resource "proxmox_virtual_environment_container" "musicbrainz" {
  node_name   = var.pve_node
  vm_id       = 101
  description = "MusicBrainz mirror"
  tags        = ["docker", "media"]

  unprivileged  = true
  start_on_boot = true

  operating_system {
    template_file_id = proxmox_download_file.debian_ct.id
    type             = "debian"
  }

  cpu {
    cores = 12
  }

  memory {
    dedicated = 16384
    swap      = 512
  }

  disk {
    datastore_id = "local-zfs"
    size         = 350
  }

  network_interface {
    name     = "eth0"
    bridge   = "vmbr0"
    firewall = false
  }

  initialization {
    hostname = "musicbrainz"
    ip_config {
      ipv4 {
        address = "192.168.1.197/24"
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

  startup {
    order = 2
  }

  provisioner "local-exec" {
    command = "ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/common.yml ../playbooks/provision/musicbrainz.yml --vault-password-file ~/.config/ansible-on-nest/vault-pass --limit musicbrainz"
  }

  lifecycle {
    ignore_changes = [
      operating_system,
      console,
    ]
  }
}
