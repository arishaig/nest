provider "proxmox" {
  endpoint  = var.pve_endpoint
  api_token = var.pve_api_token
  insecure  = true

  ssh {
    agent    = true
    username = "root"

    node {
      name    = "proxmox"
      address = "192.168.1.16"
    }
  }
}

provider "adguard" {
  host     = "${var.adguard_host}:${var.adguard_port}"
  username = var.adguard_username
  password = var.adguard_password
  scheme   = var.adguard_scheme
  insecure = true
}

provider "vultr" {
  api_key     = var.vultr_api_key
  rate_limit  = 100
  retry_limit = 3
}

provider "cloudflare" {
  api_token = var.cf_api_token
}

provider "adguard" {
  alias    = "secondary"
  host     = "${var.adguard_secondary_host}:80"
  username = var.adguard_username
  password = var.adguard_password
  scheme   = "http"
  insecure = true
}

# Tertiary (VPS) — use scripts/apply-tertiary.sh which manages the tunnel automatically.
# AdGuard listens on localhost:3000 (HTTP); tunnel forwards to local :13000.
provider "adguard" {
  alias    = "tertiary"
  host     = "${var.adguard_tertiary_host}:${var.adguard_tertiary_port}"
  username = var.adguard_username
  password = var.adguard_password
  scheme   = var.adguard_tertiary_scheme
  insecure = true
}
