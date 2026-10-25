variable "cloudflare_account_id" {
  type = string
}

variable "cloudflare_zone_id" {
  type        = string
  description = "Zone ID of gavinf.com."
}

variable "domain" {
  type    = string
  default = "gavinf.com"
}

variable "api_subdomain" {
  type        = string
  default     = "easydocs-api"
  description = "Where the tunnel serves the API. One level under the zone, so Universal SSL covers it."
}

variable "site_origin" {
  type        = string
  default     = "https://easydocs.gavinf.com"
  description = "Where the pages are served. The only origin allowed to upload to the bucket."
}

variable "bucket_name" {
  type    = string
  default = "easydocs-uploads"
}
