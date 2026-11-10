# easydocs on AWS: implementation plan

Port easydocs to AWS: Route 53, CloudFront, S3, API Gateway, Lambda, DynamoDB and Cognito, all
in Terraform, deployed from GitHub Actions with no long-lived keys.

**Status: not started, and not next.** This follows the Cloudflare rewrite
(`docs/cloudflare-plan.md` on the `cloudflare` branch). It assumes that rewrite has shipped: the
API is TypeScript on Hono, the data is in D1 and R2, and sign-in is Cloudflare Access. If the
port starts from the FastAPI/Postgres version instead, sections 3 to 6 still hold; only WP0 and
the reader half of the migration script (WP9) change.

AWS limits, prices and feature names below are from memory, not checked against current docs.
Section 9 lists what to verify before building. Do that first.

## 1. Target architecture

```
 browser ──► easydocs.gavinf.com          (CNAME in Cloudflare DNS → easydocs.aws.gavinf.com)
               │
        Route 53 zone aws.gavinf.com      (delegated from the Cloudflare zone with NS records)
               │
        CloudFront distribution           ACM certificate (us-east-1), security headers policy
          │
          ├─ default  ──► S3 "site" bucket (private, OAC)        pages, CSS, JS. No docs in them.
          │                 CloudFront Function: page routes (/docs/* → /static/viewer.html ...)
          │
          └─ /api/* /admin/api/* /uploads/* /auth/*
                ──► API Gateway HTTP API ──► Lambda "easydocs-api" (Hono, Node, arm64)
                      │ CloudFront Function: session cookie → Authorization header
                      │ JWT authorizer (Cognito) on everything except /auth/*
                      │
                      ├─ DynamoDB table "easydocs"   docs, folders, settings, upload metadata
                      ├─ S3 "uploads" bucket         file bytes; browser PUTs and GETs by presigned URL
                      └─ Cognito user pool           managed login, email one-time code or passkey

 GitHub Actions ── OIDC role ──► terraform, Lambda code, S3 sync, CloudFront invalidation
 CloudWatch alarms ──► SNS ──► email            AWS Budgets ──► email
```

What changes for the user: the sign-in page is Cognito's instead of Cloudflare's. It still asks
for an email and sends a code, so the work laptop needs no other account signed in.

### Decisions (with the reason, so they can be revisited)

| Decision | Why |
| --- | --- |
| Keep Hono; run it on Lambda with `hono/aws-lambda` | routes, path helpers and tests carry over; only storage, files and auth are rewritten |
| Sessions are Cognito tokens in HttpOnly cookies, set by the Lambda (a small backend-for-frontend) | `<img>` tags cannot send a header, and the pages already assume cookie auth after the Cloudflare rewrite |
| A CloudFront Function copies the cookie into `Authorization` | lets API Gateway's built-in JWT authorizer do the check; unauthenticated requests never start a Lambda |
| Uploads go browser → S3 by presigned PUT; downloads are a 302 to a presigned GET | Lambda responses cap at 6 MB and HTTP API payloads at 10 MB, so 25 MB files cannot pass through |
| DynamoDB single table, on-demand | the store has had a pk/sk shape since the Postgres version; on-demand avoids throttling on large docs for pennies |
| Doc bodies are keyed by an immutable doc id, not by path | DynamoDB cannot rename a key; a folder move must not rewrite every body |
| One region, us-east-1 | CloudFront certificates must live there anyway; closest to the owner |
| HTTP API, not REST API | cheaper, simpler, has the JWT authorizer; nothing here needs REST API features |
| Terraform, same S3 state backend as today | already in use in this repo |
| Route 53 hosts `aws.gavinf.com`, delegated from Cloudflare | `gavinf.com` stays on Cloudflare for the other projects; the delegated zone can serve their AWS ports too |
| No WAF at first | about $5 a month minimum; API throttling, Lambda reserved concurrency and Cognito cover a one-user app |

### Limits that shape the code

| Limit | Value | Consequence |
| --- | --- | --- |
| DynamoDB item size | 400 KB | docs stay capped at 350 KB; body and metadata are separate items |
| DynamoDB Query page | 1 MB | paginate every Query; listing must not touch bodies |
| DynamoDB TransactWriteItems | 100 items, 4 MB | big folder moves run in chunks and must be safe to re-run |
| DynamoDB BatchWriteItem | 25 items, may return unprocessed | loop until empty |
| Lambda response (buffered) | 6 MB | no file bytes through Lambda |
| API Gateway HTTP API | 10 MB payload, 30 s timeout | fine for 350 KB docs |
| CloudFront Function | about 1 ms CPU, 10 KB code, no network | string work only: rewrites and header copies |
| Cookie | about 4 KB each | ID token and refresh token go in separate cookies |
| Cognito built-in email | about 50 a day | fine for one user; SES otherwise |

## 2. Repository layout on the `aws` branch

```
src/
  app.ts            builds the Hono app from a Platform (store, blobs, identify)   ← WP0
  paths.ts          unchanged
  routes/docs.ts    unchanged apart from taking the store from the Platform
  routes/uploads.ts upload/download go through the Blobs interface
  platform/
    types.ts        Store, Blobs, Identify interfaces
    aws/
      lambda.ts     entry: handle(app) from hono/aws-lambda
      store.ts      DynamoDB
      blobs.ts      S3 presigning, head, delete
      auth.ts       /auth/* routes, claims → owner, origin check
    local/
      server.ts     @hono/node-server against DynamoDB Local + MinIO, dev identity
public/             pages (as on Cloudflare; _redirects and _headers are replaced by CloudFront)
edge/
  page-routes.js    CloudFront Function, default behaviour
  auth-header.js    CloudFront Function, API behaviours
infra/aws/          Terraform (section 7)
scripts/
  d1_to_dynamo.py   one-off data migration (uv)
test/               the same HTTP-level suite, run against the local platform
compose.yml         DynamoDB Local + MinIO, pinned tags
```

`src/platform/cloudflare/` (the D1 store, R2 streaming, Access JWT check) exists after WP0 and is
deleted in WP11.

## 3. Data model (DynamoDB)

One table, `easydocs`. `PK` (string), `SK` (string). On-demand billing, point-in-time recovery
on, deletion protection on, TTL attribute `ttl`.

`<o>` is the owner: the user's lowercased, verified email, as on Cloudflare.

| PK | SK | Attributes | Notes |
| --- | --- | --- | --- |
| `<o>#DOC` | doc path | `id`, `title`, `updated`, `uploads` (list of names) | small; this is what `tree` and `home` read |
| `<o>#BODY` | doc `id` | `content` | one GetItem to open a doc; never listed except by search |
| `<o>#FOLDER` | folder path | | so empty folders persist |
| `<o>#SITE` | `HOME` / `SEEDED` | home settings, `uploads` | |
| `<o>#FILE` | upload name | `content_type`, `size`, `object_key`, `etag`, `status`, `created`, `modified`, `folder`, `ttl` (pending only) | the `uploads` table |
| `<o>#UPLOADFOLDER` | folder path | | |
| `<o>#REF` | `<upload name>#<doc id or @home>` | | "used in" and rename, without scanning docs |

How the operations map:

- **Tree, home:** Query `<o>#DOC` and `<o>#FOLDER` (paginate). Bodies are never read.
- **Open a doc:** GetItem `DOC` by path, then GetItem `BODY` by id.
- **Save a doc:** one TransactWriteItems: Put `BODY`, Put `DOC`, and Put/Delete `REF` items for the
  difference between the old `uploads` list and the names found in the new content.
  "Create, fail if exists" is `ConditionExpression: attribute_not_exists(PK)` on the `DOC` put.
- **Move a doc:** transaction: Put `DOC` at the new path (condition: not exists), Delete the old.
  The body and refs are keyed by id, so they do not move.
- **Move a folder:** Query the `DOC` and `FOLDER` items under the prefix
  (`begins_with(SK, "<src>/")`), then transactions of up to 50 moves each (a Put and a Delete per
  item). More than 50 items is not atomic: write the new items before deleting the old, in path
  order, so a retry after a failure completes the move and loses nothing.
- **Search:** Query `<o>#BODY` page by page and match in the Lambda, building the same snippet as
  today. This reads every body: fine for hundreds of docs, and the known scaling limit.
- **Uploads list:** Query `<o>#FILE`, `<o>#REF` and `<o>#DOC` (for id → path); join in memory.
- **Rename an upload:** Query `REF` by `begins_with(SK, "<name>#")`, relink those bodies, and
  write the new `FILE` item, deleted old one and rewritten refs in transactions.
- **Stale pending uploads:** pending `FILE` items carry a `ttl` 24 hours out, so DynamoDB removes
  the row. `start_upload` still deletes the S3 objects of pending rows older than a day, and a
  lifecycle rule aborts incomplete multipart uploads.

Uploads are unique per owner now (the partition is the owner), not globally. Names carry a
random tag, and every lookup is owner-scoped, so nothing depends on global uniqueness.

## 4. Files (S3)

Two private buckets, both with Block Public Access on, SSE-S3, a TLS-only bucket policy.

- **site:** readable only by the CloudFront distribution (origin access control). HTML and
  `/static/*` are uploaded with `Cache-Control: no-cache` (they are not fingerprinted), `vendor/`
  with a long max-age.
- **uploads:** keys are `<owner hash>/<uuid><ext>`. Versioning on, non-current versions expire
  after 30 days (an accidental delete is recoverable). CORS allows `PUT` with `Content-Type` from
  the site's origins only.

Upload flow (the three calls the pages already make):

1. `POST /admin/api/uploads/start` → validate type and size, write a pending `FILE` item, return
   `upload: {method: "PUT", url: <presigned, 15 min, Content-Type signed>, headers}`.
2. The browser PUTs the file straight to S3.
3. `POST /admin/api/uploads/{name}/finish` → HeadObject; missing → 400; over 25 MB → delete and
   413; else mark ready with the real size and etag, clear `ttl`, file into the folder.

`GET /uploads/{name}` → look up the caller's `FILE` item, 302 to a presigned GET with
`ResponseContentType` set from the item. Sign with the time rounded down to the hour and a
two-hour expiry, and send `Cache-Control: private, max-age=3600` on the redirect, so the URL is
stable for an hour and the browser's cache works (the trick `storage.py` used). Files are served
from S3's hostname again, so an uploaded SVG cannot run script on the app's origin.

## 5. Auth (Cognito)

### User pool

- Sign-in with email. Self sign-up off: users are created by the admin (Terraform or CLI).
- First factors allowed: email one-time code and passkey. No passwords if the pool allows it;
  otherwise a password plus required TOTP.
- Managed login on a Cognito prefix domain to start (free, no certificate).
- One app client with a secret, authorization-code flow with PKCE, scopes `openid email`.
  Callback and sign-out URLs for both hostnames and `http://localhost:8787`.
- ID token lifetime 1 hour, refresh token 7 days, token revocation on. Deletion protection on.

### Routes the Lambda adds (no authorizer on these)

| Route | Does |
| --- | --- |
| `GET /auth/login?next=` | sets a short-lived `__Host-login` cookie (state, PKCE verifier, safe `next`), redirects to Cognito |
| `GET /auth/callback` | checks state, exchanges the code, verifies the ID token (`aws-jwt-verify`), requires `email_verified`, sets the session cookies, redirects to `next` |
| `POST /auth/refresh` | uses the refresh cookie to get a new ID token; 401 if it fails |
| `POST /auth/logout` | revokes the refresh token, clears cookies, redirects to Cognito's `/logout` |

Cookies: `__Host-id` (ID token; `HttpOnly; Secure; SameSite=Lax; Path=/`) and `__Host-refresh`
(same flags; only read by `/auth/*`). The client secret lives in SSM Parameter Store as a
SecureString, read once per cold start.

### Request path for everything else

1. CloudFront Function `auth-header.js`: if `__Host-id` is present, set
   `Authorization: Bearer <value>`. Nothing else.
2. API Gateway JWT authorizer: issuer is the user pool, audience is the app client id. No valid
   token → 401 from the gateway; the Lambda does not run.
3. Lambda middleware: owner = `claims.email` lowercased; reject if `email_verified` is not true
   or the email is not in the `ALLOWED_EMAILS` parameter (same defence in depth as on Cloudflare).
   Then the same-origin check for non-GET requests (still the CSRF defence).

The API Gateway URL is reachable directly, bypassing CloudFront. That is acceptable: it still
demands a valid token on every route but `/auth/*`.

### In the pages

`api()` in `common.js`: on a 401, `POST /auth/refresh` once and retry; if that fails, viewer and
admin pages go to `/auth/login?next=<here>`, and the editor shows its "sign in again" banner
(which opens `/auth/login` in a new tab) so unsaved text is not lost. While a page is open,
refresh every 50 minutes so images keep loading. Sign out posts to `/auth/logout`.

## 6. Edge (CloudFront)

- Alternate names `easydocs.aws.gavinf.com` and `easydocs.gavinf.com`; one ACM certificate with
  both, DNS-validated (one validation record in Route 53, one in the Cloudflare zone).
- TLS 1.2 minimum, HTTP/2 and 3, IPv6 on, price class 100.
- Response headers policy on every behaviour: HSTS, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`.
- Behaviours, in order: `/admin/api/*`, `/api/*`, `/uploads/*`, `/auth/*` → API origin, caching
  disabled, all methods, forward everything but `Host`, `auth-header.js` on viewer request.
  Default → site bucket, `page-routes.js` on viewer request.
- `page-routes.js` replaces `_redirects`: `/` and `/docs/*` → `/static/viewer.html`; `/admin`,
  `/admin/`, `/admin/uploads`, `/admin/home` → `/static/admin.html`; `/admin/edit` and
  `/admin/edit/*` → `/static/editor.html`. Anything else passes through.

## 7. Infrastructure as code

`infra/aws/`, one root module, split by file:

| File | Resources |
| --- | --- |
| `dns.tf` | Route 53 zone `aws.gavinf.com`; NS delegation and the cutover CNAME in the Cloudflare zone (cloudflare provider) |
| `cert.tf` | ACM certificate and validation |
| `site.tf` | site bucket, OAC, bucket policy |
| `uploads.tf` | uploads bucket, CORS, versioning, lifecycle |
| `table.tf` | DynamoDB table |
| `auth.tf` | user pool, domain, app client, admin-created users from `var.allowed_emails`, SSM parameters |
| `api.tf` | Lambda (arm64, 512 MB, 10 s timeout, reserved concurrency 10), its role, log group (14 days), HTTP API, routes, JWT authorizer, stage throttling |
| `edge.tf` | CloudFront distribution, the two functions, headers policy |
| `ci.tf` | GitHub OIDC provider, deploy role scoped to this repo and `main` |
| `alarms.tf` | SNS topic; alarms on Lambda errors, Lambda duration p95, API 5xx, DynamoDB throttles and system errors; a monthly budget alert |

The Lambda role may: read and write the one table, get/put/delete objects under the uploads
bucket, read its own SSM parameters, write its own log group. Nothing else.

Terraform creates the function with a placeholder package and ignores later code changes; the
pipeline owns the code.

### Pipeline (`.github/workflows/`)

- **Every push and PR:** typecheck, tests (DynamoDB Local and MinIO as service containers),
  `terraform fmt -check`, `validate`, and a `plan` posted to the PR.
- **Merge to `main`:** build with esbuild → zip → `update-function-code` → `publish-version` →
  move the `live` alias; `s3 sync public/` to the site bucket; CloudFront invalidation of `/*`.
  The API Gateway integration points at the `live` alias.
- **Infrastructure changes:** `terraform apply` behind a GitHub environment with manual approval.
- **Roll back code:** point the `live` alias at the previous version (a `make rollback` target).

## 8. Work packages

Same rules as the Cloudflare plan: stay in your files, nothing touches production without the
owner, hand off with what was verified and how.

```
WP0 seams ──┬─► WP2 DynamoDB store ──┐
            ├─► WP3 S3 blobs ────────┤
WP1 infra ──┼─► WP4 Cognito auth ────┼─► WP7 tests + local dev ─► WP8 pipeline + alarms ─► WP10 cutover ─► WP11 decommission
foundation  ├─► WP5 Lambda + edge ───┤
            ├─► WP6 frontend ────────┘
            └─► WP9 data migration
```

| WP | Owns | Done when |
| --- | --- | --- |
| **0: platform seams** (can merge to `main` while still on Cloudflare) | `src/app.ts`, `src/platform/types.ts`, moves of the D1/R2/Access code into `src/platform/cloudflare/` | routes import only the interfaces; the Cloudflare site deploys and its tests pass unchanged; the HTTP test suite takes a platform factory |
| **1: infra foundation** | `infra/aws/` except `api.tf`, `edge.tf` | `plan` is clean; after a human `apply`: zone delegated, certificate issued, buckets, table and user pool exist, the owner can sign in on the managed login page with an email code |
| **2: DynamoDB store** | `src/platform/aws/store.ts`, its tests | every `Store` function passes the shared store tests against DynamoDB Local, including pagination past 1 MB, a 120-doc folder move interrupted halfway and re-run, and two owners |
| **3: S3 blobs** | `src/platform/aws/blobs.ts`, the `Blobs` use in `routes/uploads.ts` | presigned PUT and GET work against MinIO; oversize upload is removed on finish; hour-stable GET URL |
| **4: Cognito auth** | `src/platform/aws/auth.ts`, its tests | login, callback, refresh, logout tested against a stub token endpoint; state mismatch, unverified email, disallowed email and bad `next` are rejected; cookies have the flags in section 5 |
| **5: Lambda and edge** | `src/platform/aws/lambda.ts`, `edge/*`, `infra/aws/api.tf`, `edge.tf`, build script | unit tests for both CloudFront Functions; on a staging stack, `/` loads, `/api/tree` is 401 without a cookie and 200 with one |
| **6: frontend** | `public/` | 401 → refresh → retry; sign-out; upload to an absolute presigned URL; the manual checklist from the Cloudflare plan's WP5 passes locally |
| **7: tests and local dev** | `test/`, `compose.yml`, `src/platform/local/`, `Makefile` | `make dev` runs the whole app locally with no AWS account; the full HTTP suite is green |
| **8: pipeline and alarms** | `.github/workflows/`, `infra/aws/ci.tf`, `alarms.tf` | a merge deploys with no stored AWS keys; a forced Lambda error sends an email; `make rollback` works on staging |
| **9: data migration** | `scripts/d1_to_dynamo.py` | from a `wrangler d1 export`: items, bodies (new ids), files and refs written; R2 objects copied to S3 under the new keys; `--verify` compares counts, content hashes and object existence; rehearsed into staging |
| **10: cutover** (owner-led) | | see below |
| **11: decommission** | deletions | Cloudflare Worker, D1, R2 bucket and Access app removed after a final D1 export and a week of clean running; `src/platform/cloudflare/` deleted; README rewritten |

### Cutover

1. Stop editing. Export D1; run the migration and `--verify` against production.
2. Sign in at `easydocs.aws.gavinf.com` from a personal device and the work laptop; walk the
   checklist.
3. In the Cloudflare zone: remove the Worker's custom domain for `easydocs.gavinf.com`, add the
   DNS-only CNAME to `easydocs.aws.gavinf.com` (Terraform, `dns.tf`).
4. Smoke test on `easydocs.gavinf.com`.

Rollback within the first week: remove the CNAME, re-attach the Worker's custom domain. Edits
made on AWS since cutover are not carried back.

## 9. Verify before building

- **Cognito email one-time code as a first factor:** which feature plan it needs, and whether it
  works with Cognito's built-in email or requires SES. If SES: verify the domain, and note that
  the SES sandbox only sends to verified addresses (enough for one user).
- **Cognito:** whether a pool can be passwordless-only; managed login versus the classic hosted
  UI in the Terraform provider; current token lifetime bounds.
- **API Gateway JWT authorizer:** that it accepts Cognito ID tokens by `aud` and still cannot
  read cookies (if it can, drop `auth-header.js`).
- **Runtime:** the current Node LTS Lambda runtime; current `hono/aws-lambda` entry shape for
  HTTP API payload format 2.0.
- **Cost.** Expected to be about a dollar a month, almost all of it the Route 53 zone:

  | Service | Expectation to confirm |
  | --- | --- |
  | Route 53 hosted zone | about $0.50 a month, no free tier |
  | Lambda, CloudFront, Cognito, CloudWatch (10 alarms), ACM, SSM standard parameters | within the always-free allowances at this usage |
  | DynamoDB on-demand, with point-in-time recovery | cents |
  | S3 | cents per GB; free only in an account's first year |
  | API Gateway HTTP API | about $1 per million requests after the first year |
  | CloudFront flat-rate free plan | check whether it bundles WAF and DNS at no cost; if so, adopt it and add WAF |

- **Free-tier terms** for the account in use (AWS changed them for accounts opened after
  mid-2025).

## 10. What this adds over the Cloudflare version

Worth stating in the README when it ships: alerting (the current "known gap"), 35-day
point-in-time recovery instead of 7, recoverable deletes for uploads, keyless CI with a scoped
role, versioned one-command rollback, and every resource in Terraform. What it gives up: a
single-vendor setup, zero cold starts, and a strictly $0 bill.
