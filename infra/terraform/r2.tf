# The bucket for uploaded files. It's private: the API hands out presigned URLs
# (app/storage.py), so nothing in it is reachable without one.

resource "cloudflare_r2_bucket" "uploads" {
  account_id = var.cloudflare_account_id
  name       = var.bucket_name
  location   = "enam" # eastern North America
}

# The browser PUTs files straight to the bucket with a presigned URL, which is a
# cross-origin request, so the bucket must allow it from the site. Viewing files
# (<img> tags following a redirect) needs no CORS.
resource "cloudflare_r2_bucket_cors" "uploads" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.uploads.name
  rules = [{
    allowed = {
      methods = ["PUT"]
      origins = [var.site_origin]
      headers = ["Content-Type"]
    }
    max_age_seconds = 3600
  }]
}
