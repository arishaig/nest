terraform {
  required_version = ">= 1.5"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "0.109.0"
    }
    adguard = {
      source  = "gmichels/adguard"
      version = "= 1.7.0"
    }
    null = {
      source  = "hashicorp/null"
      version = "= 3.3.0"
    }
    vultr = {
      source  = "vultr/vultr"
      version = "= 2.31.2"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "= 5.19.1"
    }
  }
}
