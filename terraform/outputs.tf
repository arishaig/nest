output "lxc_ips" {
  description = "IP addresses of all LXC containers"
  value = {
    docker      = "192.168.1.158"
    musicbrainz = "192.168.1.197"
    fileserver  = "192.168.1.17"
    scrutiny    = "192.168.1.46"
    seedbox     = "192.168.1.182"
    monitoring  = "192.168.1.44"
  }
}

output "vm_names" {
  description = "VM names and IDs"
  value = {
    homeassistant = proxmox_virtual_environment_vm.homeassistant.vm_id
    backup        = proxmox_virtual_environment_vm.backup.vm_id
  }
}

output "dns_rewrites_count" {
  description = "Number of AdGuard DNS rewrites managed"
  value       = length(local.dns_rewrites)
}
