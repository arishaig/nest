# ──────────────────────────────────────────────
# AdGuard Secondary — DNS Rewrites & Configuration
# Mirrors adguard.tf exactly; no TLS (internal-only resolver)
# ──────────────────────────────────────────────

resource "adguard_rewrite" "dns_secondary" {
  provider = adguard.secondary
  for_each = local.dns_rewrites
  domain   = each.key
  answer   = each.value

  depends_on = [proxmox_virtual_environment_container.dns_secondary]
}

resource "adguard_config" "secondary" {
  provider = adguard.secondary

  dns = {
    upstream_dns              = ["127.0.0.1:5335"]
    bootstrap_dns             = ["127.0.0.1:5335", "::1:5335"]
    rate_limit                = 0
    blocking_mode             = "refused"
    cache_size                = 4194304
    cache_ttl_min             = 0
    cache_ttl_max             = 0
    cache_optimistic          = true
    dnssec_enabled            = true
    use_private_ptr_resolvers = true
    local_ptr_upstreams       = ["127.0.0.1:5335"]
    allowed_clients = [
      "192.168.1.0/24",
      "192.168.2.0/24",
      "192.168.3.0/24",
      "192.168.4.0/24",
      "192.168.7.0/24",
    ]
  }

  stats = {
    enabled  = true
    interval = 2160
  }

  filtering = {
    enabled         = true
    update_interval = 24
  }

  querylog = {
    enabled  = true
    interval = 2160
  }

  tls = {
    enabled           = false
    server_name       = ""
    certificate_chain = ""
    private_key       = ""
  }

  depends_on = [proxmox_virtual_environment_container.dns_secondary]
}

resource "adguard_list_filter" "hagezi_pro_secondary" {
  provider = adguard.secondary
  name     = "HaGeZi's Pro Blocklist"
  url      = "https://adguardteam.github.io/HostlistsRegistry/assets/filter_48.txt"
  enabled  = true

  depends_on = [proxmox_virtual_environment_container.dns_secondary]
}

resource "adguard_list_filter" "hagezi_gambling_secondary" {
  provider = adguard.secondary
  name     = "HaGeZi's Gambling Blocklist"
  url      = "https://adguardteam.github.io/HostlistsRegistry/assets/filter_47.txt"
  enabled  = true

  depends_on = [proxmox_virtual_environment_container.dns_secondary]
}

resource "adguard_list_filter" "hagezi_threat_intel_secondary" {
  provider = adguard.secondary
  name     = "HaGeZi's Threat Intelligence Feeds"
  url      = "https://adguardteam.github.io/HostlistsRegistry/assets/filter_44.txt"
  enabled  = true

  depends_on = [proxmox_virtual_environment_container.dns_secondary]
}
