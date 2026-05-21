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

provider "adguard" {
  alias    = "secondary"
  host     = "${var.adguard_secondary_host}:3000"
  username = var.adguard_username
  password = var.adguard_password
  scheme   = "http"
  insecure = true
}
