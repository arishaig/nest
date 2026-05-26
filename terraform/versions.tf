terraform {
  required_version = ">= 1.5"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.78"
    }
    adguard = {
      source  = "gmichels/adguard"
      version = "~> 1.6"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
    vultr = {
      source  = "vultr/vultr"
      version = "~> 2.23"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }
}
