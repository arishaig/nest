# ──────────────────────────────────────────────
# AdGuard Tertiary — VPS (dns3.arishaig.site)
# DoH/DoT only — no plain DNS, no local rewrites
# ──────────────────────────────────────────────

resource "adguard_config" "tertiary" {
  provider = adguard.tertiary

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
    allowed_clients           = ["0.0.0.0/0", "::/0"]
  }

  stats = {
    enabled         = true
    interval        = 2160
    ignored_enabled = false
  }

  filtering = {
    enabled         = true
    update_interval = 24
  }

  dhcp = {
    enabled   = false
    interface = ""
    ipv4_settings = {
      gateway_ip     = "10.0.0.1"
      subnet_mask    = "255.255.255.0"
      range_start    = "10.0.0.100"
      range_end      = "10.0.0.200"
      lease_duration = 86400
    }
    ipv6_settings = {
      range_start    = "fe80::"
      lease_duration = 86400
    }
  }

  querylog = {
    enabled         = true
    interval        = 2160
    ignored_enabled = false
  }

  # TLS not managed by Terraform — provider switches to https://localhost:8443 mid-apply
  # and loses the SSH tunnel. Configure once via UI: Settings > Encryption settings.
  # Cert path: /opt/AdGuardHome/ssl/{fullchain,privkey}.pem (managed by certbot)

  depends_on = [vultr_instance.vps_proxy]
}

resource "adguard_list_filter" "hagezi_pro_tertiary" {
  provider = adguard.tertiary
  name     = "HaGeZi's Pro Blocklist"
  url      = "https://adguardteam.github.io/HostlistsRegistry/assets/filter_48.txt"
  enabled  = true

  depends_on = [vultr_instance.vps_proxy]
}

resource "adguard_list_filter" "hagezi_gambling_tertiary" {
  provider = adguard.tertiary
  name     = "HaGeZi's Gambling Blocklist"
  url      = "https://adguardteam.github.io/HostlistsRegistry/assets/filter_47.txt"
  enabled  = true

  depends_on = [vultr_instance.vps_proxy]
}

resource "adguard_list_filter" "hagezi_threat_intel_tertiary" {
  provider = adguard.tertiary
  name     = "HaGeZi's Threat Intelligence Feeds"
  url      = "https://adguardteam.github.io/HostlistsRegistry/assets/filter_44.txt"
  enabled  = true

  depends_on = [vultr_instance.vps_proxy]
}
