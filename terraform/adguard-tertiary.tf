# ──────────────────────────────────────────────
# AdGuard Tertiary — VPS (dns3.arishaig.site)
# DoT (853) and DoH (443 via Traefik SNI passthrough) for everyone; plain
# DNS (53) only from the home WAN IP and WireGuard peers — enforced by the
# nftables ruleset in playbooks/provision/vps.yml. No local rewrites.
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

  # TLS must be set here: when `tls` is omitted the provider applies its
  # defaults (enabled = false) on every apply, which silently disabled DoT/DoH
  # from 2026-06-04 until this was added. force_https stays false so the
  # provider keeps talking plain HTTP on :3000 through the SSH tunnel, and
  # HTTPS sits on 8443 because Traefik owns 443 and passes dns3 SNI through.
  # Certs are renewed by certbot and copied in by its deploy hook (adguard.yml).
  tls = {
    enabled           = true
    server_name       = "dns3.arishaig.site"
    force_https       = false
    port_https        = 8443
    port_dns_over_tls = 853
    certificate_chain = "/opt/AdGuardHome/ssl/fullchain.pem"
    private_key       = "/opt/AdGuardHome/ssl/privkey.pem"
    serve_plain_dns   = true
  }

  depends_on = [vultr_instance.vps_proxy]
}

resource "adguard_user_rules" "tertiary" {
  provider = adguard.tertiary
  rules = [
    "@@||stats.grafana.org^",
  ]

  depends_on = [vultr_instance.vps_proxy]
}

resource "adguard_list_filter" "hagezi_pro_tertiary" {
  provider = adguard.tertiary
  name     = "HaGeZi's Pro Blocklist"
  url      = "https://adguardteam.github.io/HostlistsRegistry/assets/filter_48.txt"
  enabled  = true

  depends_on = [vultr_instance.vps_proxy]
}
