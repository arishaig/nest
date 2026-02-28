# ──────────────────────────────────────────────
# PVE Firewall Configuration
# ──────────────────────────────────────────────
# The audit shows no cluster.fw and no per-VM .fw files with rules.
# The firewall flag is enabled on net0 for LXC 104 (seedbox) and
# LXC 105 (monitoring), and VM 500 (backup), but no PVE-level
# firewall rules are defined.
#
# nftables rules inside the containers are managed by Docker and
# are handled by the Ansible provisioning playbooks, not Terraform.
#
# If PVE firewall rules are added in the future, they would go here:
#
# resource "proxmox_virtual_environment_cluster_firewall" "main" {
#   enabled = true
# }
#
# resource "proxmox_virtual_environment_firewall_rules" "seedbox" {
#   node_name = var.pve_node
#   vm_id     = 104
#
#   rule {
#     action  = "ACCEPT"
#     type    = "in"
#     proto   = "tcp"
#     dport   = "22"
#     comment = "SSH"
#   }
# }
