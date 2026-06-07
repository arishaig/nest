# ──────────────────────────────────────────────
# LXC 110: foundry — FoundryVTT game server
# ──────────────────────────────────────────────

# Create /Tank/foundry_assets on the PVE host before the container starts.
# The bind mount fails on first boot if the source path is missing.
# Ownership 102000:102000 = PVE subuid base (100000) + foundry UID inside LXC (2000).
resource "null_resource" "foundry_tank_dir" {
  provisioner "local-exec" {
    command = "ssh -o StrictHostKeyChecking=no -i ~/.ssh/ansible-on-nest root@192.168.1.16 'mkdir -p /Tank/foundry_assets && chown 102000:102000 /Tank/foundry_assets'"
  }
}

resource "proxmox_virtual_environment_container" "foundry" {
  depends_on = [null_resource.foundry_tank_dir]

  node_name   = var.pve_node
  vm_id       = 110
  description = "FoundryVTT game server"
  tags        = ["foundry"]

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
    size         = 16
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
        address = "192.168.1.20/24"
        gateway = var.gateway
      }
    }
    dns {
      servers = var.dns_servers
    }
    user_account {
      keys = [
        for k in ["~/.ssh/id_ed25519.pub", "~/.ssh/ansible-on-nest.pub"] :
        trimspace(file(pathexpand(k))) if fileexists(pathexpand(k))
      ]
    }
  }

  startup {
    order = 3
  }

  provisioner "local-exec" {
    # Bind mounts (type=bind) require root@pam and cannot be set via API token.
    # We stop the container, apply the mount via pct set, then restart before Ansible.
    command = <<-EOF
      ssh -o StrictHostKeyChecking=no -i ~/.ssh/ansible-on-nest root@192.168.1.16 \
        'pct stop 110 && sleep 3 && pct set 110 -mp0 /Tank/foundry_assets,mp=/opt/foundrydata/Data/assets && pct start 110'
      timeout 180 sh -c 'until ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i ~/.ssh/ansible-on-nest root@192.168.1.20 true 2>/dev/null; do sleep 5; done'
      ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/common.yml ../playbooks/provision/foundry.yml --vault-password-file ~/.config/ansible-on-nest/vault-pass --ssh-extra-args '-o StrictHostKeyChecking=no' --limit foundry
    EOF
  }

  lifecycle {
    ignore_changes = [
      operating_system,
      console,
      initialization,
    ]
  }
}
