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
    template_file_id = proxmox_virtual_environment_download_file.debian12_ct.id
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
        address = "dhcp"
      }
    }
    dns {
      servers = [var.dns_server]
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
    command = "ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/musicbrainz.yml --limit musicbrainz"
  }

  lifecycle {
    ignore_changes = [
      operating_system,
      description,
      console,
    ]
  }
}
