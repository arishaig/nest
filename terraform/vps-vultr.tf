# ──────────────────────────────────────────────
# VPS proxy — Vultr Seattle
# Debian 13 (trixie), Traefik TCP passthrough, WireGuard tunnel to Nest
# ──────────────────────────────────────────────

resource "vultr_ssh_key" "isaac_laptop" {
  name    = "isaac-laptop"
  ssh_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBM7Y+b/BRai//GJ7Sczob/rX8ck3ArKGQP/jsR5OlWn isaac@isaaclaptop"
}

resource "vultr_ssh_key" "ansible_on_nest" {
  name    = "ansible-on-nest"
  ssh_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMxOgEao2c+jTY4NdsEp46M9Fs8i1Yx6WiX42rUAXSJh ansible-on-nest"
}

data "vultr_os" "debian" {
  filter {
    name   = "name"
    values = ["Debian 13 x64 (trixie)"]
  }
}

data "vultr_plan" "small" {
  filter {
    name   = "id"
    values = ["vc2-1c-1gb"]
  }
}

resource "vultr_instance" "vps_proxy" {
  plan      = data.vultr_plan.small.id
  region    = "sea"
  os_id     = data.vultr_os.debian.id
  label     = "vps-proxy"
  hostname  = "vps-proxy"
  ssh_key_ids = [vultr_ssh_key.isaac_laptop.id, vultr_ssh_key.ansible_on_nest.id]

  enable_ipv6           = false
  backups               = "disabled"
  ddos_protection       = false
  activation_email      = false
}

resource "null_resource" "vps_provision" {
  depends_on = [vultr_instance.vps_proxy]

  triggers = {
    instance_id = vultr_instance.vps_proxy.id
  }

  provisioner "local-exec" {
    command = <<-EOT
      sleep 30
      ansible-playbook \
        -i ../inventory/hosts.yml \
        ../playbooks/provision/vps.yml \
        -e "ansible_host=${vultr_instance.vps_proxy.main_ip} vps_public_ip=${vultr_instance.vps_proxy.main_ip}" \
        --vault-password-file ~/.config/ansible-on-nest/vault-pass \
        --limit vps-proxy
    EOT
    environment = {
      ANSIBLE_HOST_KEY_CHECKING = "False"
    }
  }
}

output "vps_ip" {
  value       = vultr_instance.vps_proxy.main_ip
  description = "VPS public IP — update Cloudflare A record and Nest Traefik trustedIPs on cutover"
}
