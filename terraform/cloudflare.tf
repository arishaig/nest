# ──────────────────────────────────────────────
# Cloudflare DNS — arishaig.site
# Wildcard → VPS proxy; specific overrides for non-VPS records.
# casa.arishaig.site is managed by Nabu Casa — not in Terraform.
# ──────────────────────────────────────────────

locals {
  cf_zone_id = "2efafcd9b998f6525b525236a26bdcce"
}

# All public subdomains → VPS (Traefik TCP passthrough → WireGuard → Nest)
resource "cloudflare_dns_record" "wildcard" {
  zone_id = local.cf_zone_id
  name    = "*"
  type    = "A"
  content = vultr_instance.vps_proxy.main_ip
  ttl     = 300
  proxied = false
}

# AdGuard Home — direct to home IP, bypasses VPS
resource "cloudflare_dns_record" "dns" {
  zone_id = local.cf_zone_id
  name    = "dns"
  type    = "A"
  content = "50.47.227.169"
  ttl     = 300
  proxied = false
}

# UDM WireGuard VPN — direct to home IP, bypasses VPS
resource "cloudflare_dns_record" "vpn" {
  zone_id = local.cf_zone_id
  name    = "vpn"
  type    = "A"
  content = "50.47.227.169"
  ttl     = 300
  proxied = false
}
