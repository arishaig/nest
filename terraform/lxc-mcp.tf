# ──────────────────────────────────────────────
# LXC 109: mcp — Nest MCP HTTP server
# ──────────────────────────────────────────────

resource "proxmox_virtual_environment_container" "mcp" {
  node_name   = var.pve_node
  vm_id       = 109
  description = "Nest MCP HTTP server"
  tags        = ["mcp"]

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
    dedicated = 512
    swap      = 256
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
    hostname = "mcp"
    ip_config {
      ipv4 {
        address = "192.168.1.19/24"
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
    order = 1
  }

  provisioner "local-exec" {
    command = <<-EOF
      timeout 180 sh -c 'until ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -i ~/.ssh/ansible-on-nest root@192.168.1.19 true 2>/dev/null; do sleep 5; done'
      ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/common.yml ../playbooks/provision/mcp.yml --vault-password-file ~/.config/ansible-on-nest/vault-pass --ssh-extra-args '-o StrictHostKeyChecking=no' --limit mcp
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
