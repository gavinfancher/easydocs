# Cloudflare Tunnel: cloudflared in the cluster dials out to Cloudflare, so
# nothing at home needs an open port or a public IP.
# https://easydocs-api.gavinf.com → tunnel → the cloudflared pod → the easydocs Service.
# The pages (frontend/, on easydocs.gavinf.com) call it there; the API allows that origin with CORS.

locals {
  api_host = "${var.api_subdomain}.${var.domain}"
  # The Service in deploy/k8s/api.yaml, by its cluster DNS name.
  api_service = "http://easydocs.easydocs.svc.cluster.local:8000"
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "api" {
  account_id = var.cloudflare_account_id
  name       = "easydocs-k8s" # "easydocs" is the old VM's tunnel
  config_src = "cloudflare"   # routes live here, not in a file in the cluster
}

# The token cloudflared runs with. Output below; it goes into Infisical, which puts it
# in the cluster's `easydocs` Secret.
data "cloudflare_zero_trust_tunnel_cloudflared_token" "api" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.api.id
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "api" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.api.id
  config = {
    ingress = [
      { hostname = local.api_host, service = local.api_service },
      { service = "http_status:404" }, # anything else
    ]
  }
}

resource "cloudflare_dns_record" "api" {
  zone_id = var.cloudflare_zone_id
  name    = var.api_subdomain
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.api.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1 # "automatic"; required for proxied records
}
