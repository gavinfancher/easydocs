# easydocs on Cloudflare (free tier): implementation plan

Rewrite easydocs so everything runs on Cloudflare's free tier: one Worker (pages + API), D1 for
docs, R2 for uploads, Cloudflare Access for sign-in. The home cluster, Postgres, the tunnel,
Infisical and the container image all go away.

This document is the spec. Each work package (WP) at the end is sized for one agent, lists the
files it owns, and has acceptance criteria. Read sections 1 to 6 before starting any WP.

Platform facts below were checked against Cloudflare's docs on 2026-11-09. Re-check anything you
depend on (`developers.cloudflare.com`), especially package names and Terraform attribute names.

## 1. Target architecture

```
 browser ──► easydocs.gavinf.com
               │
               ├─ Cloudflare Access (edge)    one-time PIN to an allowed email; 1-week session
               │                              unauthenticated requests never reach anything below
               │
               └─ Worker "easydocs"
                    ├─ static assets          /static/*, page routes via _redirects (no Worker code runs)
                    └─ src/index.ts (Hono)    /api/*, /admin/api/*, /uploads/*
                         ├─ verifies the Access JWT on every request (fails closed)
                         ├─ D1  "easydocs"           docs, folders, settings, upload metadata
                         └─ R2  "easydocs-uploads"   file bytes (binding; no S3 keys, no presigning)
```

What changes for the user: the login page becomes Cloudflare's (email → 6-digit code). Usernames
become email addresses. Everything else looks the same.

### Decisions already made (do not reopen)

| Decision | Why |
| --- | --- |
| Cloudflare Access with one-time PIN, not passwords | scrypt cannot run in 10 ms of CPU; the owner wants no third-party account signed in on a work laptop |
| TypeScript + Hono, not Python Workers | psycopg cannot come along; Pyodide start-up does not fit the CPU limit |
| One Worker, one hostname | Access cookie covers pages, API and `<img>` requests; no CORS, no presigned URLs |
| Uploads stream through the Worker | removes R2 API keys and the bucket CORS rule; 25 MB is far under the 100 MB body limit |
| Keep the `owner` column (now an email) | costs nothing; a second email added to the Access policy gets its own space |
| Keep API paths and JSON shapes | the frontend stays almost untouched and the old tests become the spec |
| Deploys stay on Workers Builds | already wired to the repo; GitHub Actions only runs tests |

### Free-tier limits that shape the code

| Limit | Value | Consequence |
| --- | --- | --- |
| Worker CPU per request | 10 ms | never loop over doc contents in JS; do multi-doc work in SQL |
| Worker requests | 100,000 / day | static assets are free and do not count; only `run_worker_first` paths do |
| D1 queries per invocation | 50 | no query-in-a-loop; `db.batch()` counts as one |
| D1 bound parameters per query | 100 | chunk `IN (...)` lists |
| D1 `LIKE`/`GLOB` pattern | 50 bytes | never use `LIKE`; use range scans and `instr()` |
| D1 SQL statement length | 100 KB | always bind values; never inline doc content into SQL |
| D1 string / row size | 2 MB | fine: docs are capped at 350 KB |
| D1 rows read / written | 5 M / 100 k per day, hard-enforced since 2026-09-01 | queries fail until midnight UTC if exceeded; index everything |
| D1 database size | 500 MB | fine |
| D1 Time Travel | 7 days | this is the backup story |
| R2 | 10 GB, 1 M writes, 10 M reads per month | fine |
| External subrequests | 50 per invocation | only the JWKS fetch is external, cached per isolate |

## 2. Repository layout after the rewrite

```
src/
  index.ts        app wiring: middleware order, route mounting, error handler
  env.ts          Env type (bindings + vars)
  auth.ts         Access JWT verification, dev identity, origin check
  store.ts        all D1 access (the only file that writes SQL)
  paths.ts        pure helpers ported from main.py (cleanRel, titleOf, fileSlug, buildTree, ...)
  routes/docs.ts      tree, docs, folders, move, home, search, config, session
  routes/uploads.ts   start, data, finish, list, rename, delete, move, upload folders, GET /uploads/:name
public/
  _redirects      page routes (moved from frontend/_redirects, minus /admin/login)
  _headers        security headers for pages
  static/         moved from frontend/static (git mv, keep history)
migrations/
  0001_init.sql
test/             vitest, runs inside workerd with local D1 + R2
scripts/
  build-codemirror.sh   moved from frontend/scripts
  pg_to_d1.py           one-off data migration (uv)
infra/terraform/  D1, R2, Access; the tunnel is removed last
wrangler.jsonc  package.json  tsconfig.json  vitest.config.ts  Makefile  README.md
```

`backend/`, `deploy/` and `frontend/` (what is left of it) stay in place until WP9. Argo CD syncs
`deploy/k8s` from `main`; deleting it early would remove the rollback target.

## 3. Configuration mock-ups

### wrangler.jsonc

```jsonc
{
  "$schema": "./node_modules/wrangler/config-schema.json",
  "name": "easydocs",
  "main": "src/index.ts",
  "compatibility_date": "2026-11-01",
  // Only the custom domain. A workers.dev or preview URL would be a second way in.
  "workers_dev": false,
  "preview_urls": false,
  "routes": [{ "pattern": "easydocs.gavinf.com", "custom_domain": true }],
  "assets": {
    "directory": "./public",
    "html_handling": "none",
    "not_found_handling": "none",
    // Everything else is a static file and never invokes the Worker.
    "run_worker_first": ["/api/*", "/admin/api/*", "/uploads/*"]
  },
  "d1_databases": [
    { "binding": "DB", "database_name": "easydocs", "database_id": "<terraform output>", "migrations_dir": "migrations" }
  ],
  "r2_buckets": [{ "binding": "UPLOADS", "bucket_name": "easydocs-uploads" }],
  "vars": {
    "SITE_TITLE": "easydocs",
    "ACCESS_TEAM_DOMAIN": "https://<team>.cloudflareaccess.com",
    "ACCESS_AUD": "<terraform output>"
  },
  "observability": { "enabled": true },
  "env": {
    "staging": {
      "routes": [{ "pattern": "easydocs-staging.gavinf.com", "custom_domain": true }],
      "d1_databases": [{ "binding": "DB", "database_name": "easydocs-staging", "database_id": "<...>", "migrations_dir": "migrations" }],
      "r2_buckets": [{ "binding": "UPLOADS", "bucket_name": "easydocs-uploads-staging" }],
      "vars": { "SITE_TITLE": "easydocs (staging)", "ACCESS_TEAM_DOMAIN": "https://<team>.cloudflareaccess.com", "ACCESS_AUD": "<staging aud>" }
    }
  }
}
```

Secret (not in the repo, which may be public): `ALLOWED_EMAILS`, a comma-separated list, set with
`wrangler secret put ALLOWED_EMAILS` for both environments. Local dev uses `.dev.vars`
(gitignored) with `DEV_USER=dev@localhost` and no `ACCESS_*` values.

### migrations/0001_init.sql

```sql
-- Docs, folders and settings. Same pk/sk layout as the Postgres `items` table:
--   DOC <path> | FOLDER <path> | SITE HOME | UPLOAD <file name> | UPLOADFOLDER <path>
-- `data` is small JSON (title, updated, folder, home settings). A doc's markdown is in `content`,
-- declared last so listing titles never reads it.
CREATE TABLE items (
  owner   TEXT NOT NULL,
  pk      TEXT NOT NULL,
  sk      TEXT NOT NULL,
  data    TEXT NOT NULL DEFAULT '{}',
  content TEXT,
  PRIMARY KEY (owner, pk, sk)
);

CREATE TABLE uploads (
  name         TEXT PRIMARY KEY,            -- the URL: /uploads/<name>; unique across owners
  owner        TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size         INTEGER NOT NULL,
  object_key   TEXT NOT NULL UNIQUE,        -- key in the R2 bucket; never changes on rename
  etag         TEXT,
  status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready')),
  created      INTEGER NOT NULL,            -- unix seconds
  modified     INTEGER NOT NULL
);
CREATE INDEX uploads_owner ON uploads (owner, status);

-- Which doc links which upload, so "used in" and rename never scan every doc.
-- src is a doc path, or '@home' for the home page body.
CREATE TABLE refs (
  owner TEXT NOT NULL,
  name  TEXT NOT NULL,
  src   TEXT NOT NULL,
  PRIMARY KEY (owner, name, src)
) WITHOUT ROWID;
CREATE INDEX refs_src ON refs (owner, src);
```

No `users` or `sessions` tables: Access owns both.

### infra/terraform (sketch; verify attribute names against provider `~> 5.28` docs)

```hcl
resource "cloudflare_d1_database" "easydocs" {
  account_id = var.cloudflare_account_id
  name       = "easydocs"
}

resource "cloudflare_zero_trust_access_identity_provider" "otp" {
  account_id = var.cloudflare_account_id
  name       = "One-time PIN"
  type       = "onetimepin"
  config     = {}
}

resource "cloudflare_zero_trust_access_policy" "owner" {
  account_id = var.cloudflare_account_id
  name       = "easydocs owners"
  decision   = "allow"
  include    = [for e in var.allowed_emails : { email = { email = e } }]   # tfvars, gitignored
}

resource "cloudflare_zero_trust_access_application" "easydocs" {
  account_id                 = var.cloudflare_account_id
  name                       = "easydocs"
  type                       = "self_hosted"
  domain                     = "easydocs.gavinf.com"
  session_duration           = "168h"
  allowed_idps               = [cloudflare_zero_trust_access_identity_provider.otp.id]
  auto_redirect_to_identity  = true      # one login method: skip the chooser
  http_only_cookie_attribute = true
  same_site_cookie_attribute = "lax"
  app_launcher_visible       = false
  policies                   = [{ id = cloudflare_zero_trust_access_policy.owner.id, precedence = 1 }]
}

output "access_aud"     { value = cloudflare_zero_trust_access_application.easydocs.aud }
output "d1_database_id" { value = cloudflare_d1_database.easydocs.id }
```

Same again for staging (`easydocs-staging.gavinf.com`, its own D1 and bucket). `r2.tf` keeps the
bucket and drops `cloudflare_r2_bucket_cors` (nothing PUTs to the bucket from a browser any more).
`tunnel.tf` and the `easydocs-api` DNS record are removed in WP9, not before.

## 4. Auth and security design

Access stops unauthenticated requests at the edge. The Worker still verifies every request
itself, so a deleted or misconfigured Access application means "nothing works", not "open to all".

### src/auth.ts (shape, not final code)

```ts
import { createRemoteJWKSet, jwtVerify } from "jose";

let jwks: ReturnType<typeof createRemoteJWKSet> | undefined;   // cached per isolate

/** The signed-in user's email (lowercased), or null. Never throws. */
export async function identify(req: Request, env: Env): Promise<string | null> {
  if (!env.ACCESS_AUD || !env.ACCESS_TEAM_DOMAIN) {
    // Local dev only: both conditions must hold. Production always has ACCESS_AUD.
    const host = new URL(req.url).hostname;
    return env.DEV_USER && (host === "localhost" || host === "127.0.0.1") ? env.DEV_USER : null;
  }
  const token = req.headers.get("Cf-Access-Jwt-Assertion") ?? cookie(req, "CF_Authorization");
  if (!token) return null;
  try {
    jwks ??= createRemoteJWKSet(new URL(`${env.ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs`));
    const { payload } = await jwtVerify(token, jwks, {
      issuer: env.ACCESS_TEAM_DOMAIN, audience: env.ACCESS_AUD, algorithms: ["RS256"],
    });
    const email = typeof payload.email === "string" ? payload.email.trim().toLowerCase() : "";
    if (!email) return null;                                    // service tokens carry no email
    const allowed = (env.ALLOWED_EMAILS ?? "").split(",").map(s => s.trim().toLowerCase()).filter(Boolean);
    return allowed.length && !allowed.includes(email) ? null : email;
  } catch { return null; }
}
```

`ctx.access` is not available to Workers with static assets (the internal assets router does not
pass it on), which is why the JWT is verified by hand.

### Middleware order in src/index.ts

1. `identify()`; no user → `401 {"detail": "Sign in first"}`. Applies to every Worker route. There
   are no open routes.
2. Origin check: for any method other than GET/HEAD, the `Origin` header must equal the request's
   own origin, else `403`. Auth is now an ambient cookie, so this is the CSRF defence.
3. Route handler, with the owner passed explicitly into every store call (no globals: isolates
   are shared across requests).
4. Error handler: `HttpError(status, detail)` → `{"detail": ...}` with that status (the frontend
   reads `detail`). Anything else → 500 with a generic detail and a `console.error`.

Response headers: API responses `Cache-Control: no-store`; uploads as described in section 5.

### Uploads are now same-origin

Before, files were served from R2's own hostname, so a hostile SVG could not touch the app. Now
`/uploads/x.svg` is on the app's origin. Every `/uploads/*` response must carry:

```
Content-Security-Policy: default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox
X-Content-Type-Options: nosniff
Cache-Control: private, max-age=3600
```

and `Content-Type` comes from the `uploads` row (set from the extension allowlist at upload
time), never from the client or from R2 metadata.

### public/_headers

```
/*
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Referrer-Policy: same-origin
```

A script CSP for the pages is out of scope (they use inline scripts); DOMPurify stays as is.

### Session expiry in the browser

When the Access session ends, a `fetch()` gets a redirect to the Access login host. In
`common.js`, `api()` must call `fetch(url, { ...opts, redirect: "manual" })` and treat
`res.type === "opaqueredirect"` or `res.status === 401` as "session ended":

- viewer and admin pages: `location.reload()` (a top-level navigation goes through Access login).
- editor with unsaved changes: do not reload. Show a banner with a "Sign in again" link that
  opens `/` in a new tab; when the tab regains focus, retry the save.

Sign out is `location.assign("/cdn-cgi/access/logout")`.

## 5. API contract

Paths, methods, request bodies, response bodies and status codes stay exactly as in
`backend/app/main.py` unless listed here. That file is the reference; port behaviour, not style.

| Route | Change |
| --- | --- |
| `POST /api/login`, `POST /api/logout` | removed |
| `GET /api/session` | kept: always `{"login_required": true, "signed_in": true, "user": "<email>"}` |
| `POST /api/uploads/links` | removed (images load from `/uploads/<name>` directly) |
| `POST /admin/api/uploads` (multipart) | removed (no page uses it; it would buffer 25 MB in memory) |
| `GET /uploads/{name}` | no `expires`/`sig`; streams from R2 instead of redirecting |
| `PUT /admin/api/uploads/{name}/data` | now the only upload path; streams to R2 |
| `GET /healthz`, `GET /readyz` | removed (nothing can probe through Access) |
| invalid JSON body | `422 {"detail": "..."}` (what FastAPI returned) |

### Store rules (src/store.ts)

- Every function takes `owner` as its first argument. No ambient owner.
- Prefix queries use a range, not `LIKE`: `sk >= ?1 AND sk <= ?1 || char(1114111)`
  (compute the upper bound in TS as `prefix + "\u{10FFFF}"`; the default BINARY collation
  matches Postgres `COLLATE "C"` ordering).
- "Create, fail if exists": `INSERT ... ON CONFLICT DO NOTHING`, then `meta.changes === 0` → `Exists`.
- Multi-statement writes go in one `db.batch([...])`: it is a transaction and one query against
  the 50-per-invocation limit.
- Listing docs (`tree`, `home`) selects `sk, json_extract(data,'$.title'), json_extract(data,'$.updated')`
  and never `content`.
- Folder move is two `UPDATE`s in a batch (items, refs) that rewrite the prefix in SQL:
  `SET sk = ?dst || substr(sk, length(?src) + 1)` over the range. No rows travel to the Worker.
  The existing conflict checks (404, 400 into-itself, 409 exists) run first.
- Search runs in SQL and returns the snippet from SQL:
  `instr(lower(content), ?q)` for the match and `substr(content, max(1, idx - 60), 160)` for the
  snippet; `LIMIT 50`. SQLite `lower()` is ASCII-only, a small accepted change from Python.
- `refs` is maintained in the same batch as every doc write, move and delete, and on home-page
  save: extract `/uploads/([\w.-]+)` from the one body being saved, replace that src's rows.
- Upload rename: look up referencing docs through `refs`, fetch only those contents in one
  query, relink in JS with the same regex as `rename_upload` in main.py, write back in one batch.
- `GET /admin/api/uploads`: `used_in` and `on_home` come from a join on `refs`.
- A new owner's welcome doc: when `GET /api/tree` finds no `SITE/SEEDED` item for the owner,
  batch-insert the welcome doc (if they have no docs) and the marker.

### Upload flow (three calls, same as today's "local" path, so `uploadFile()` barely changes)

1. `POST /admin/api/uploads/start` → validates type and declared size, clears up to 20 stale
   pending uploads (older than 24 h: delete the object, then the row), inserts a `pending` row,
   returns `{name, file, upload: {method: "PUT", url: "/admin/api/uploads/<file>/data", headers: {"Content-Type": ...}}}`.
2. `PUT /admin/api/uploads/{name}/data` → row must be the caller's and `pending`. Reject if
   `Content-Length` is missing or over 25 MB (413). Then
   `env.UPLOADS.put(key, request.body, { httpMetadata: { contentType } })`: streamed, never
   buffered (R2 needs a known length, which `Content-Length` provides). If the stored size is over
   the limit, delete the object and row and return 413. Otherwise mark `ready` with size and etag.
3. `POST /admin/api/uploads/{name}/finish` → 400 if still `pending`; files it into the folder;
   returns `{url, is_image, folder}`.

`GET /uploads/{name}`: one D1 lookup (owner + `ready`, else 404), then
`env.UPLOADS.get(key, { range: request.headers, onlyIf: request.headers })`. Support `206` with
`Content-Range` (video seeking) and `304` on a matching `If-None-Match`. Send the row's
`Content-Type`, the object's `ETag`, and the headers from section 4.

## 6. Rules for every agent

- Work on branch `cloudflare`. It has uncommitted changes (`Makefile` staged, `README.md`
  modified): do not discard them. Commit only when your WP's acceptance criteria pass.
- Never run anything against production: no `--remote` on the `easydocs` database, no
  `wrangler deploy` without `--env staging`, no `terraform apply`. Those are human steps (WP8).
- No ORM, no zod. Hono, jose, and hand-written validators. Keep the bundle small: start-up
  parsing counts against CPU.
- One `JSON.parse` of the request body per request. No loops over doc contents in JS.
- Stay inside your WP's files. If you need a change in another WP's file, note it in your
  hand-off instead of editing it.
- Python (WP7 only): `uv`, never pip. Any Docker database: `postgres:18.4`.
- Keep comments in the existing voice: short, plain, explaining why.
- Hand-off for each WP: what was built, how it was verified (commands and results), anything
  that did not match this plan.

## 7. Work packages

```
WP0 scaffold + spikes ──┬─► WP1 store ───────┐
                        ├─► WP2 auth ────────┤
                        ├─► WP3 docs routes ─┼─► WP6 integration tests + CI ─► WP8 cutover (human-led) ─► WP9 decommission
                        ├─► WP4 uploads ─────┤
                        ├─► WP5 frontend ────┘
                        └─► WP7 terraform + data migration script
```

WP1 to WP5 and WP7 run in parallel once WP0 has landed. WP3 and WP4 code against the store
interface that WP0 defines and WP1 implements.

### WP0: scaffold, contracts, spikes (one agent, lands first)

Owns: `package.json`, `tsconfig.json`, `wrangler.jsonc`, `vitest.config.ts`, `src/env.ts`,
`src/index.ts` (skeleton), `src/store.ts` (exported types and function signatures only, bodies
`throw new Error("WP1")`), `migrations/0001_init.sql`, `.gitignore`, the `git mv` of
`frontend/static` → `public/static`, `frontend/_redirects` → `public/_redirects` and
`frontend/scripts/build-codemirror.sh` → `scripts/` (fix its paths).

- Pin exact versions. Test runner is `@cloudflare/vitest-plugin` (the successor to
  `@cloudflare/vitest-pool-workers`); confirm the current name and config shape in the docs.
- Define `HttpError`, `Exists`, `Missing`, the `Item` and `UploadRow` types, and one signature
  per store function listed in WP1.
- `npm run dev` = apply local migrations, then `wrangler dev`. `npm test`, `npm run typecheck`.

Spikes, on the staging Worker (needs a human to have run WP7's staging Terraform and
`wrangler login`; if not available, write the spike scripts and flag it):

| Spike | Pass | If it fails |
| --- | --- | --- |
| S1: save a 350 KB doc (parse, title, refs, D1 write) | CPU under 10 ms in Workers Logs across 20 runs | lower the doc cap; report numbers |
| S2: PUT a 25 MB file to R2 through the Worker, then a ranged GET | succeeds; CPU under 10 ms | report; fallback is presigned PUT (keeps R2 keys) |
| S3: behind Access, a request to `/api/session` | `Cf-Access-Jwt-Assertion` present; jose verify under 5 ms warm; `_redirects` page routes still served without invoking the Worker | use the `CF_Authorization` cookie; report |

Done when: `npm test` runs one trivial test in workerd with D1 and R2 bindings; `npm run dev`
serves `/` from `public/`; spike results are written into the hand-off.

### WP1: store

Owns: `src/store.ts`, `test/store.test.ts`.

Implement, per the rules in section 5: `query`, `anyWithPrefix`, `get`, `put` (with `isNew`),
`del`, `batchWrite`, `moveDoc`, `moveFolder`, `search`, `recentDocs`, `seedWelcome`; `refs`
maintenance inside doc and home writes; uploads: `uploadExists`, `listUploads` (with `used_in`,
`on_home`), `getUpload`, `startUpload`, `finishUpload`, `renameUpload`, `deleteUpload`,
`staleUploads`.

Done when: unit tests cover every function, including two owners never seeing each other's
rows, a path longer than 50 bytes in a prefix query, `%` and `_` in paths, a 350 KB doc
round-trip, and a 150-name `IN` list.

### WP2: auth and security middleware

Owns: `src/auth.ts`, `test/auth.test.ts`, `public/_headers`.

Done when tests (signing JWTs with a test key and a stubbed JWKS fetch) show: no token → 401;
bad signature, wrong `aud`, wrong `iss`, expired → 401; token without `email` → 401; email not
in `ALLOWED_EMAILS` → 401; valid → owner is the lowercased email; cookie fallback works; dev
identity works only on localhost with `ACCESS_AUD` unset, and a production-shaped env with
`DEV_USER` set still returns 401; POST without a matching `Origin` → 403; the JWKS is fetched
once across many requests.

### WP3: docs routes

Owns: `src/paths.ts`, `src/routes/docs.ts`, `test/paths.test.ts`.

Port from main.py: `clean_rel`, `parents`, `title_of`, `doc_item` (350 KB cap, 413),
`build_tree` (folders before docs, case-insensitive by name), `HOME_DEFAULTS`, `WELCOME`, and the
routes `config`, `tree`, `docs` (GET/POST/PUT/DELETE), `move`, `folders` (POST/DELETE), `home`
(GET/PUT), `search`, `session`. Mind the Python details: `title_of` uses the first line matching
`^#\s+(.+)`, and `str.capitalize()` lowercases the rest of the string.

Done when: `paths.test.ts` covers each helper with the cases from `test_api.py`
(`test_bad_paths_rejected`, `test_doc_size_limit`, titles) and the routes typecheck against the
store interface.

### WP4: uploads routes

Owns: `src/routes/uploads.ts`, `test/upload-helpers.test.ts`.

Port: `CONTENT_TYPES`, `IMAGE_EXTS`, `upload_type`, `file_slug`, `new_upload_name`,
`clean_upload_folder`, `with_parents`, `reparent`, `load_upload_folders` / `save_upload_folders`
(write only what changed), and the routes in section 5's upload flow plus list, rename, delete,
move, and the three upload-folder routes. The rename regex and alt-text relinking must match
`rename_upload` exactly.

Done when: helper tests pass, including rename cases with a size suffix in the alt text
(`![name|50%](...)`).

### WP5: frontend

Owns: everything under `public/`, except `_headers`.

- Delete `login.html`, `auth.js`, `config.js`; remove their `<script>` tags from `viewer.html`,
  `admin.html`, `editor.html`; delete the `/admin/login` line from `_redirects`. Delete
  `frontend/config.production.js` and `frontend/package.json`'s build (no build step remains).
- `common.js`: `api()` as in section 4; remove `API_BASE`, `markUploads`, `fetchUploadLinks`,
  `resolveUploads` and their callers so `/uploads/...` URLs go straight into `src`/`href`
  (keep the loading placeholder). `admin.js:507` `data-upload` → `src`. `editor.html` around
  line 330 no longer needs to map links back to paths.
- `uploadFile()` in `admin.js`: always the same-origin PUT; drop the presigned branch.
- Sign-out button: always visible; goes to `/cdn-cgi/access/logout`.
- Editor: the "session ended" banner and retry described in section 4.

Done when: with `npm run dev`, every page loads with no console errors; create, edit, autosave,
move and delete a doc; paste an image in the editor; upload, rename, file and delete on the
uploads tab; search; a mermaid block renders. `grep -rn "API_BASE\|auth\.\|data-upload" public/static --include=*.js --include=*.html`
returns nothing outside `vendor/`.

### WP6: integration tests and CI (after WP1 to WP5)

Owns: `test/api.test.ts`, `.github/workflows/ci.yml`, `Makefile`.

Port `backend/tests/test_api.py` as HTTP-level tests against the Worker in workerd:

| Old test | New |
| --- | --- |
| docs_crud_and_tree, bad_paths_rejected, doc_size_limit, folders, empty_folder_survives..., move_doc, move_folder, home_and_search | port as is |
| upload_validation, uploads_are_served, uploads_list_rename_delete, upload_folders, browser_upload_checks_type_and_size, browser_upload_to_r2, r2_upload_too_big_is_removed | port to the PUT-through-Worker flow |
| users_only_see_their_own_things | port with two emails |
| api_needs_a_session | every route returns 401 without a JWT |
| login_*, changing_a_password, signed_link, cors, presign, upload_links_*, form_upload, probes | drop |
| new | upload response headers (CSP sandbox, nosniff, content type from the row); `Range` → 206; `If-None-Match` → 304; `used_in` follows doc edits, moves and deletes; folder move keeps refs; stale pending cleanup |

- `ci.yml`: on every push and PR: `npm ci`, `npm run typecheck`, `npm test`. Remove the image and
  release jobs.
- `Makefile`: `dev`, `test`, `logs` (`wrangler tail`), `backup`
  (`wrangler d1 export easydocs --remote --output backups/<date>.sql`; add `backups/` to
  `.gitignore`), `migrate-data` (WP7's script).

Done when: `npm test` is green locally and in CI on the branch.

### WP7: Terraform and the data migration script

Owns: `infra/terraform/*`, `scripts/pg_to_d1.py`.

Terraform: add the resources in section 3 for production and staging; remove the R2 CORS rule;
leave `tunnel.tf` alone. Replace `cluster_secret_values` with `access_aud`, `d1_database_id` and
their staging twins. Update `terraform.tfvars.example` (`allowed_emails`, team domain). The API
token additionally needs: Access: Apps and Policies Write; Access: Organizations, Identity
Providers, and Groups Write; D1: Edit. `terraform validate` and `terraform plan` only.

`scripts/pg_to_d1.py` (run with `infisical run --env=prod -- uv run ...`, like `uploads-to-r2`):

- Arguments: `--database-id`, repeated `--map <old owner>=<email>`, `--dry-run`. Refuses to run
  if any owner in Postgres is unmapped, if any `uploads` row has no `object_key` (run
  `make uploads-to-r2` first), or if the target tables are not empty.
- `items` → `items`: for `DOC`, `content` moves out of the JSON into the `content` column; other
  kinds copy `data` as is. `uploads` → `uploads`: skip `pending` rows; timestamps to unix seconds;
  drop `bucket` and `data`. Build `refs` from every doc body and the home body. Insert a
  `SITE/SEEDED` marker per owner. `users` and `sessions` are not migrated.
- Write through the D1 HTTP API with bound parameters (inlined SQL would exceed the 100 KB
  statement limit for large docs). No account, user or email values in logs.
- `--verify`: per owner and kind, row counts match; sha256 of every doc's content matches;
  every `object_key` exists in the bucket (HEAD with the existing R2 keys from Infisical).

Done when: a rehearsal into the staging D1 from a local `postgres:18.4` seeded with test data
(including a 350 KB doc and two owners) passes `--verify`.

### WP8: cutover (human-led; an agent may prepare and assist, never initiate)

Prerequisites the owner does by hand:

1. Zero Trust team name known; API token permissions extended; `terraform.tfvars` filled in.
2. `terraform apply` (creates D1, Access; Access now also fronts the current static site, which
   keeps working: its API is on `easydocs-api.gavinf.com`).
3. Put the two Terraform outputs into `wrangler.jsonc`; `wrangler secret put ALLOWED_EMAILS`.
4. `wrangler deploy --env staging`; walk the WP5 checklist on `easydocs-staging.gavinf.com`
   from a personal device and from the work laptop (one-time PIN only).

Cutover:

1. Stop editing. `wrangler d1 migrations apply easydocs --remote`.
2. `make migrate-data` with the owner mapping, then `--verify`.
3. Workers Builds settings: root directory `/`, build command empty, deploy command
   `npx wrangler d1 migrations apply easydocs --remote && npx wrangler deploy`.
4. Merge `cloudflare` → `main`. Wait for the build.
5. Smoke test: home page, an old doc with images, search, edit and save, upload, sign out and
   back in. Confirm `https://easydocs.gavinf.com/api/tree` returns the Access login when signed out.

Rollback (any time in the first week): revert the merge commit, set the Workers Builds root
directory back to `frontend` with its old commands. The cluster API and Postgres are untouched,
so the old site is back as it was. Edits made on the new site since cutover are not carried back.

### WP9: decommission (one week after a clean cutover)

Owns: deletions only. `kubectl delete -f deploy/argocd/easydocs.yaml`; remove `backend/`,
`deploy/`, `frontend/`, `.infisical.yaml`, `infra/terraform/tunnel.tf` (then `terraform apply`,
human); delete the R2 API token, the Infisical project, the GHCR package, the staging Worker's
test data. Take a final `pg_dump` of the `easydocs` database before dropping it. Rewrite
`README.md` for the new shape: the diagram in section 1, how to add a user (add an email to
`allowed_emails` and `ALLOWED_EMAILS`), `make` targets, and the "Known gaps" section updated
(backups: 7-day Time Travel plus `make backup`; no replica or throttle caveats; still no alerting).
