output "api_url" {
  value = "https://${local.api_host}"
}

# The Terraform-made part of the cluster's `easydocs` Secret. Hidden in
# plan/apply output; copy it into Infisical (easydocs project, prod) from:
#
#   terraform output -json cluster_secret_values
#
# The rest you add there yourself; see the README.
output "cluster_secret_values" {
  sensitive = true
  value = {
    TUNNEL_TOKEN           = data.cloudflare_zero_trust_tunnel_cloudflared_token.api.token
    EASYDOCS_R2_ACCOUNT_ID = var.cloudflare_account_id
    EASYDOCS_R2_BUCKET     = cloudflare_r2_bucket.uploads.name
  }
}
