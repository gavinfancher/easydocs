terraform {
  required_version = ">= 1.11"

  # Where state lives. Values come from backend.hcl:
  #   terraform init -backend-config=backend.hcl
  backend "s3" {}

  required_providers {
    cloudflare = { source = "cloudflare/cloudflare", version = "~> 5.28" }
  }
}

# Credentials come from environment variables, so no secret is ever written
# in this directory. The S3 state backend uses your normal AWS credentials.

provider "cloudflare" {} # CLOUDFLARE_API_TOKEN
