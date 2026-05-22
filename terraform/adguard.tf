# ──────────────────────────────────────────────
# AdGuard Home — DNS Rewrites & Configuration
# ──────────────────────────────────────────────

locals {
  dns_rewrites = {
    # Local-access names — direct IP, bypass Traefik entirely (infra break-glass)
    "proxmox.local.arishaig.site"    = "192.168.1.16"
    "backup.local.arishaig.site"     = "192.168.1.113"
    "monitoring.local.arishaig.site" = "192.168.1.44"
    "casa.local.arishaig.site"       = "192.168.4.50"
    "docker.local.arishaig.site"     = "docker.arishaig.site"
    "adguard.local.arishaig.site"    = "dns.arishaig.site"
    "files.local.arishaig.site"      = "files.arishaig.site"

    # Local-access names — Traefik route without Authelia (see external-services.yml)
    "torrent.local.arishaig.site"     = "192.168.1.158"
    "scrutiny.local.arishaig.site"    = "192.168.1.158"
    "backlight.local.arishaig.site"   = "192.168.1.158"
    "musicbrainz.local.arishaig.site" = "192.168.1.158"
    "storyteller.local.arishaig.site" = "192.168.1.158"

    # Direct-IP entries (no Traefik conflict)
    "backlight.arishaig.site"   = "192.168.4.97"
    "files.arishaig.site"       = "192.168.1.17"
    "musicbrainz.arishaig.site" = "192.168.1.197"
    "adguard.arishaig.site"     = "dns.arishaig.site"
    "dns.arishaig.site"         = "192.168.7.7"
    "dns2.arishaig.site"        = "192.168.1.53"

    # Traefik reverse-proxied services (all -> docker host)
    "monitoring.arishaig.site"   = "192.168.1.158"
    "jellyfin.arishaig.site"     = "192.168.1.158"
    "seerr.arishaig.site"        = "192.168.1.158"
    "requests.arishaig.site"     = "192.168.1.158"
    "sonarr.arishaig.site"       = "192.168.1.158"
    "radarr.arishaig.site"       = "192.168.1.158"
    "bazarr.arishaig.site"       = "192.168.1.158"
    "lidarr.arishaig.site"       = "192.168.1.158"
    "prowlarr.arishaig.site"     = "192.168.1.158"
    "nzbd.arishaig.site"         = "192.168.1.158"
    "storyteller.arishaig.site"  = "192.168.1.158"
    "dash.arishaig.site"         = "192.168.1.158"
    "uptime.arishaig.site"       = "192.168.1.158"
    "glances.arishaig.site"      = "192.168.1.158"
    "mealie.arishaig.site"       = "192.168.1.158"
    "copyparty.arishaig.site"    = "192.168.1.158"
    "recommendarr.arishaig.site" = "192.168.1.158"
    "watcharr.arishaig.site"     = "192.168.1.158"
    "tunarr.arishaig.site"       = "192.168.1.158"
    "auth.arishaig.site"         = "192.168.1.158"
    "torrent.arishaig.site"      = "192.168.1.158"
    "proxmox.arishaig.site"      = "192.168.1.158"
    "scrutiny.arishaig.site"     = "192.168.1.158"
    "backup.arishaig.site"       = "192.168.1.158"
    "watchback.arishaig.site"    = "192.168.1.158"
    "medialyze.arishaig.site"    = "192.168.1.158"
    "docker.arishaig.site"       = "192.168.1.158"

    # UDM VPN
    "vpn.arishaig.site" = "192.168.1.1"
  }
}

resource "adguard_rewrite" "dns" {
  for_each = local.dns_rewrites
  domain   = each.key
  answer   = each.value
}

# --- AdGuard Config ---

resource "adguard_config" "main" {
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

  tls = {
    enabled           = true
    server_name       = "dns.arishaig.site"
    force_https       = true
    port_https        = 443
    port_dns_over_tls = 853
    certificate_chain = "/opt/AdGuardHome/ssl/fullchain.pem"
    private_key       = "/opt/AdGuardHome/ssl/privkey.pem"
    serve_plain_dns   = true
  }

  querylog = {
    enabled  = true
    interval = 2160
  }
}

# --- Filter Lists ---

resource "adguard_list_filter" "hagezi_pro" {
  name    = "HaGeZi's Pro Blocklist"
  url     = "https://adguardteam.github.io/HostlistsRegistry/assets/filter_48.txt"
  enabled = true
}

resource "adguard_list_filter" "hagezi_gambling" {
  name    = "HaGeZi's Gambling Blocklist"
  url     = "https://adguardteam.github.io/HostlistsRegistry/assets/filter_47.txt"
  enabled = true
}

resource "adguard_list_filter" "hagezi_threat_intel" {
  name    = "HaGeZi's Threat Intelligence Feeds"
  url     = "https://adguardteam.github.io/HostlistsRegistry/assets/filter_44.txt"
  enabled = true
}
