# ──────────────────────────────────────────────
# LXC 111: foundry — FoundryVTT game server
# ──────────────────────────────────────────────

resource "null_resource" "foundry_tank_dir" {
  provisioner "local-exec" {
    command = "ssh -i ~/.ssh/ansible-on-nest root@192.168.1.16 'mkdir -p /Tank/foundry'"
  }
}

resource "null_resource" "foundry_mount" {
  depends_on = [proxmox_virtual_environment_container.foundry]

  provisioner "local-exec" {
    command = <<-EOT
      ssh -i ~/.ssh/ansible-on-nest root@192.168.1.16 \
        'chown 100000:100000 /Tank/foundry && pct set 111 -mp0 /Tank/foundry,mp=/mnt/foundry && pct reboot 111'
      until ssh -i ~/.ssh/ansible-on-nest -o StrictHostKeyChecking=no -o ConnectTimeout=5 root@192.168.1.21 exit 2>/dev/null; do sleep 3; done
      ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook -i ../inventory/hosts.yml \
        ../playbooks/provision/common.yml \
        ../playbooks/provision/foundry.yml \
        --vault-password-file ~/.config/ansible-on-nest/vault-pass \
        --limit foundry
    EOT
  }
}

resource "proxmox_virtual_environment_container" "foundry" {
  depends_on = [null_resource.foundry_tank_dir]

  node_name   = var.pve_node
  vm_id       = 111
  description = "FoundryVTT game server"
  tags        = ["game", "application"]

  unprivileged  = true
  start_on_boot = true

  operating_system {
    template_file_id = proxmox_download_file.debian_ct.id
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

  features {
    nesting = true
  }

  network_interface {
    name     = "eth0"
    bridge   = "vmbr0"
    firewall = false
  }

  initialization {
    hostname = "foundry"
    ip_config {
      ipv4 {
        address = "192.168.1.21/24"
        gateway = var.gateway
      }
    }
    dns {
      servers = var.dns_servers
    }
    user_account {
      keys = var.ssh_public_keys
    }
  }

  startup {
    order    = 3
    up_delay = 5
  }

  lifecycle {
    ignore_changes = [
      operating_system,
      console,
      initialization,
      mount_point,
    ]
  }
}
