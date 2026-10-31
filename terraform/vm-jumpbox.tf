# ──────────────────────────────────────────────
# VM 113: jumpbox — Cloud IoT (VLAN 6) diagnostics
# ──────────────────────────────────────────────
# Dual-homed on purpose: net0 on the main LAN (SSH in, apt out), net1 on
# VLAN 6 so it shares an L2 with the cloud IoT devices and needs no UniFi
# rule between zones. It is the one host that touches both networks, so it
# does not forward (playbooks/provision/jumpbox.yml) and is meant to sit
# stopped except while in use. `started` is ignored below so a later apply
# never powers it back on.

resource "proxmox_download_file" "debian_cloud_image" {
  node_name    = var.pve_node
  content_type = "iso" # the `local` store has no `import` content type; .img suffix is the documented workaround
  datastore_id = "local"
  file_name    = "debian-13-genericcloud-amd64.img"
  # Browse images: https://cloud.debian.org/images/cloud/trixie/latest/
  url                 = "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"
  overwrite           = false
  overwrite_unmanaged = false
}

resource "proxmox_virtual_environment_vm" "jumpbox" {
  node_name   = var.pve_node
  vm_id       = 113
  name        = "jumpbox"
  description = "Dual-homed diagnostics host for Cloud IoT (VLAN 6). Keep stopped when idle."
  tags        = ["jumpbox", "iot"]

  on_boot = false
  started = true

  # The genericcloud image has no qemu-guest-agent. Enabling the agent here
  # would make the provider wait on it for 15 minutes every apply.
  agent {
    enabled = false
  }

  cpu {
    cores = 1
    type  = "host"
  }

  memory {
    dedicated = 1024
  }

  disk {
    datastore_id = "local-zfs"
    interface    = "scsi0"
    file_id      = proxmox_download_file.debian_cloud_image.id
    size         = 8
    discard      = "on"
    ssd          = true
  }

  scsi_hardware = "virtio-scsi-pci"

  # net0 → main LAN
  network_device {
    bridge = "vmbr0"
  }

  # net1 → Cloud IoT (VLAN 6)
  network_device {
    bridge  = "vmbr0"
    vlan_id = 6
  }

  # Cloud images expect a serial console.
  serial_device {}

  operating_system {
    type = "l26"
  }

  initialization {
    datastore_id = "local-zfs"

    ip_config {
      ipv4 {
        address = "192.168.1.22/24"
        gateway = var.gateway
      }
    }
    # No gateway on VLAN 6: the box must not route via that zone.
    # .2 is below UniFi's default DHCP pool (.6+); confirm against the
    # CheapSlop network's actual range.
    ip_config {
      ipv4 {
        address = "192.168.8.2/24"
      }
    }

    dns {
      servers = var.dns_servers
    }

    user_account {
      username = "debian"
      # Hardcoded (public keys, same as playbooks/provision/common.yml): this VM is
      # created by the CI runner, which has no ~/.ssh/*.pub to read, and an empty
      # list would leave the box with no way to log in.
      keys = [
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBM7Y+b/BRai//GJ7Sczob/rX8ck3ArKGQP/jsR5OlWn isaac@isaaclaptop",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMxOgEao2c+jTY4NdsEp46M9Fs8i1Yx6WiX42rUAXSJh ansible-on-nest",
      ]
    }
  }

  lifecycle {
    ignore_changes = [
      started,
    ]
  }
}

resource "null_resource" "jumpbox_provision" {
  depends_on = [proxmox_virtual_environment_vm.jumpbox]

  triggers = {
    vm_id = proxmox_virtual_environment_vm.jumpbox.id
  }

  provisioner "local-exec" {
    # Runs from terraform/, where Ansible does not find the repo-root ansible.cfg,
    # so host_key_checking must be disabled here for the VM's brand-new host key.
    command = "ANSIBLE_HOST_KEY_CHECKING=False ANSIBLE_VARS_ENABLED=host_group_vars,community.sops.sops ansible-playbook -i ../inventory/hosts.yml ../playbooks/provision/jumpbox.yml --limit jumpbox"
  }
}
