"""Where uploaded files' bytes live: a Cloudflare R2 bucket, reached with presigned URLs.

The browser never sees R2 credentials. To upload, it asks the API for a presigned PUT URL and
sends the file straight to R2; to show one, /uploads/<name> redirects to a presigned GET URL.
The API itself only signs URLs, plus a HEAD to check an upload arrived and a DELETE.

Presigning is AWS Signature V4 over R2's S3 API, done here with hashlib so there's no SDK
to carry around. Set all four EASYDOCS_R2_* variables to turn R2 on; without them (local dev,
tests) uploads are kept in Postgres instead (see store.py).
"""

import hashlib
import hmac
import os
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

ACCOUNT_ID = os.environ.get("EASYDOCS_R2_ACCOUNT_ID", "")
BUCKET = os.environ.get("EASYDOCS_R2_BUCKET", "")
ACCESS_KEY_ID = os.environ.get("EASYDOCS_R2_ACCESS_KEY_ID", "")
SECRET_ACCESS_KEY = os.environ.get("EASYDOCS_R2_SECRET_ACCESS_KEY", "")
ENDPOINT = os.environ.get("EASYDOCS_R2_ENDPOINT", "") or (
    f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com" if ACCOUNT_ID else "")
REGION = "auto"  # R2 has no regions; SigV4 still wants one

PUT_SECONDS = 15 * 60  # time the browser has to start sending a file


def enabled() -> bool:
    return bool(BUCKET and ACCESS_KEY_ID and SECRET_ACCESS_KEY and ENDPOINT)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _q(s: str) -> str:
    return quote(s, safe="-_.~")


def new_key(ext: str) -> str:
    """A new object's key. Random, so renaming a file (its URL) never touches the object."""
    return f"uploads/{uuid.uuid4().hex}{ext}"


def presign_url(method: str, host: str, path: str, *, access_key: str, secret_key: str, region: str,
                now: datetime, expires: int, headers: dict | None = None, query: dict | None = None,
                scheme: str = "https") -> str:
    """A SigV4 query-string-signed URL. Any `headers` are signed, so the request must send them as given."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{amz_date[:8]}/{region}/s3/aws4_request"
    signed = {"host": host, **{k.lower(): v.strip() for k, v in (headers or {}).items()}}
    names = ";".join(sorted(signed))
    params = {**(query or {}), "X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{access_key}/{scope}",
              "X-Amz-Date": amz_date, "X-Amz-Expires": str(expires), "X-Amz-SignedHeaders": names}
    qs = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(params.items()))
    canonical = "\n".join([method, quote(path, safe="/-_.~"), qs,
                           "".join(f"{k}:{signed[k]}\n" for k in sorted(signed)), names, "UNSIGNED-PAYLOAD"])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(("AWS4" + secret_key).encode(), amz_date[:8])
    for part in (region, "s3", "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{scheme}://{host}{quote(path, safe='/-_.~')}?{qs}&X-Amz-Signature={signature}"


def presign(method: str, key: str, expires: int, headers: dict | None = None, query: dict | None = None,
            now: datetime | None = None, bucket: str | None = None) -> str:
    """A presigned URL for one object (path style: <endpoint>/<bucket>/<key>)."""
    end = urlsplit(ENDPOINT)
    return presign_url(method, end.netloc, f"/{bucket or BUCKET}/{key}", access_key=ACCESS_KEY_ID,
                       secret_key=SECRET_ACCESS_KEY, region=REGION, now=now or datetime.now(timezone.utc),
                       expires=expires, headers=headers, query=query, scheme=end.scheme)


def upload_url(key: str, content_type: str) -> dict:
    """Where the browser PUTs a file. Content-Type is signed, so the file can't arrive as another type."""
    headers = {"Content-Type": content_type}
    return {"method": "PUT", "url": presign("PUT", key, PUT_SECONDS, headers), "headers": headers}


def download_url(key: str, bucket: str | None = None) -> str:
    """A presigned GET URL. It's signed as of the start of the hour and lasts two, so one file keeps
    the same URL (and stays in the browser's cache) for an hour, and every URL is good for at least one."""
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return presign("GET", key, 2 * 3600, now=hour, bucket=bucket)


def _call(method: str, key: str, data: bytes | None = None, headers: dict | None = None, bucket: str | None = None):
    url = presign(method, key, 300, headers, bucket=bucket)
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    return urllib.request.urlopen(req, timeout=60)


def head(key: str, bucket: str | None = None) -> dict | None:
    """The object's size, type and etag, or None if it isn't there."""
    try:
        with _call("HEAD", key, bucket=bucket) as r:
            return {"size": int(r.headers.get("Content-Length", 0)), "content_type": r.headers.get("Content-Type", ""),
                    "etag": (r.headers.get("ETag") or "").strip('"')}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def put(key: str, data: bytes, content_type: str) -> str:
    """Upload from the server (the old-style form upload, and moving files out of Postgres). Returns the etag."""
    with _call("PUT", key, data, {"Content-Type": content_type}) as r:
        return (r.headers.get("ETag") or "").strip('"')


def delete(key: str, bucket: str | None = None) -> None:
    try:
        _call("DELETE", key, bucket=bucket).close()
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
