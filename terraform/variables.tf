# --- Proxmox VE ---
variable "pve_endpoint" {
  description = "Proxmox VE API endpoint URL"
  type        = string
  default     = "https://192.168.1.16:8006"
}

variable "pve_api_token" {
  description = "Proxmox VE API token (user@realm!tokenid=token-value)"
  type        = string
  sensitive   = true
}

variable "pve_node" {
  description = "Proxmox VE node name"
  type        = string
  default     = "proxmox"
}

# --- AdGuard Home ---
variable "adguard_host" {
  description = "AdGuard Home IP address"
  type        = string
  default     = "192.168.7.7"
}

variable "adguard_port" {
  description = "AdGuard Home admin port"
  type        = number
  default     = 443
}

variable "adguard_scheme" {
  description = "AdGuard Home URL scheme (http or https)"
  type        = string
  default     = "https"
}

variable "adguard_username" {
  description = "AdGuard Home admin username"
  type        = string
  sensitive   = true
}

variable "adguard_password" {
  description = "AdGuard Home admin password"
  type        = string
  sensitive   = true
}

variable "adguard_secondary_host" {
  description = "Secondary AdGuard Home IP address"
  type        = string
  default     = "192.168.7.8"
}

# --- Network ---
variable "gateway" {
  description = "Default gateway IP"
  type        = string
  default     = "192.168.1.1"
}

variable "dns_server" {
  description = "DNS server IP"
  type        = string
  default     = "192.168.7.7"
}

# --- SSH ---
variable "ssh_public_keys" {
  description = "SSH public keys for provisioned containers"
  type        = list(string)
  default = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBM7Y+b/BRai//GJ7Sczob/rX8ck3ArKGQP/jsR5OlWn isaac@isaaclaptop",
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMxOgEao2c+jTY4NdsEp46M9Fs8i1Yx6WiX42rUAXSJh ansible-on-nest",
  ]
}

# --- PBS ---
variable "pbs_fingerprint" {
  description = "Proxmox Backup Server TLS fingerprint"
  type        = string
  default     = "4a:8f:b5:ed:3e:6a:4c:f8:b9:81:78:87:42:45:5c:d1:e9:37:ab:35:29:ce:f5:63:65:37:78:09:2f:d6:ed:3b"
}

variable "pbs_password" {
  description = "PBS API password for storage connection"
  type        = string
  sensitive   = true
}
