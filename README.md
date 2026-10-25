# easydocs

A small self-hosted markdown docs site, with a web editor. Live at https://easydocs.gavinf.com.

- **`/`**: the docs site (signed-in only): a home page, then sidebar, search and diagrams
- **`/admin/`**: admin panel. Organize docs and folders, and manage uploads
- **`/admin/edit`**: the editor, with live preview

## How it runs

```
 browser ──► easydocs.gavinf.com ──── the pages, CSS and JS: static files on Cloudflare (web/)
    │
    ├──────► easydocs-api.gavinf.com ── tunnel ──► cloudflared ──► easydocs (FastAPI)   k8s, namespace easydocs
    │                                                                 ├──► Postgres "easydocs" on postgres.ad.gavinf.com
    │                                                                 └──► signs R2 URLs
    └──────► R2 bucket easydocs-uploads: files PUT straight to it, and shown from it (both presigned)
```

The same shape as wishly. The pages are static files, so Cloudflare serves them from its CDN for
free, with no Worker code. They call the API on its own hostname, a Cloudflare Tunnel to the
cluster, so nothing at home has an open port. The API allows the site's origin with CORS.

The whole site needs the admin password, reading included. Signing in at `/admin/login` gets a
session token (30 days), kept in the browser and sent as `Authorization: Bearer` on every API
call. Signing out deletes it on the server. Ten wrong passwords from one IP pause logins for 15
minutes. The pages themselves hold no docs; the API checks the token on every call, and a page
whose call comes back 401 sends you to sign in.

Uploaded files live in R2, and the browser never sees R2 credentials. To upload, it asks the API
for a presigned PUT URL and sends the file straight to the bucket. Docs keep linking to
`/uploads/<name>`; since an `<img>` can't send the token, the pages swap those paths for
presigned GET URLs from `/api/uploads/links` (good for at least an hour, and the same URL
within it, so the browser caches them). Each file's name, bucket, object key, size, type and
etag are in the `uploads` table.

| Piece | Where |
| --- | --- |
| API and pages (FastAPI) | `app/main.py` |
| Postgres access, table layout | `app/store.py` |
| R2: presigned URLs (SigV4, no SDK) | `app/storage.py` |
| Admin password and sessions | `app/auth.py`, `app/static/login.html` |
| Editor text area (CodeMirror; links fold to a chip, markdown unchanged) | `app/static/text-editor.js`, rebuilt with `scripts/build-codemirror.sh` |
| The site on Cloudflare: build, page routes (`_redirects`), the API's address | `web/` |
| Kubernetes: API, cloudflared, Secret from Infisical | `deploy/k8s/` |
| Argo CD Application | `deploy/argocd/easydocs.yaml` |
| Tunnel, DNS, R2 bucket and its CORS rule | `infra/terraform/` |
| Tests, image, deploy | `.github/workflows/ci.yml` |

### Deploying

Push to `main`. Two things follow:

- **The API.** CI tests it, pushes `ghcr.io/gavinfancher/easydocs:<sha>`, and commits that tag to
  `deploy/k8s/kustomization.yaml`. Argo CD sees the commit and rolls the pod (the new one is ready
  before the old one stops). Roll back by reverting the `deploy: easydocs <sha>` commit.
- **The pages.** Cloudflare Workers Builds runs `npm run build` and `npx wrangler deploy` in `web/`.

```sh
make logs                                   # tail the API on the cluster
kubectl -n easydocs get pods                # 1 easydocs + 1 cloudflared
curl https://easydocs-api.gavinf.com/readyz # {"status":"ready"}
```

### Secrets

All in Infisical (project `easydocs`, env `prod`, at the root). The Infisical operator copies them
into the `easydocs` Secret in the cluster and restarts the pods when one changes.

| Key | Where it comes from |
| --- | --- |
| `EASYDOCS_DATABASE_URL` | `postgresql://easydocs:<password>@postgres.ad.gavinf.com/easydocs` (the same one the VM uses) |
| `EASYDOCS_ADMIN_PASSWORD_HASH` | `make password-hash`. Change the password by pasting a new one in |
| `TUNNEL_TOKEN`, `EASYDOCS_R2_ACCOUNT_ID`, `EASYDOCS_R2_BUCKET` | `terraform -chdir=infra/terraform output -json cluster_secret_values` |
| `EASYDOCS_R2_ACCESS_KEY_ID`, `EASYDOCS_R2_SECRET_ACCESS_KEY` | Cloudflare → R2 → Manage API tokens (step 2 below) |

### Setting it up (once)

1. **Terraform** makes the tunnel, `easydocs-api.gavinf.com`, the bucket and its CORS rule. R2 has
   to be turned on for the account first (Cloudflare → R2). The API token needs Account ·
   Cloudflare Tunnel: Edit, Account · Workers R2 Storage: Edit and Zone · DNS: Edit on `gavinf.com`.
   ```sh
   cd infra/terraform
   cp backend.hcl.example backend.hcl && cp terraform.tfvars.example terraform.tfvars   # fill in the IDs
   export AWS_PROFILE=... CLOUDFLARE_API_TOKEN=...
   terraform init -backend-config=backend.hcl && terraform apply
   ```
2. **R2 key for the API**: Cloudflare → R2 → Manage API tokens → Create, **Object Read & Write**,
   only the `easydocs-uploads` bucket. Keep the Access Key ID and Secret Access Key.
3. **Infisical**: make the `easydocs` project and put every key above in `prod`. Make a machine
   identity (universal auth) a Viewer on it, then store its login in the cluster (you run this, so
   the secret never goes through anyone else):
   ```sh
   read "CID?Client ID: "; read -s "CSEC?Client secret: "; echo
   kubectl -n infisical-operator-system create secret generic infisical-easydocs-identity \
     --from-literal=clientId="$CID" --from-literal=clientSecret="$CSEC"; unset CID CSEC
   ```
   Put the project's slug in `deploy/k8s/infisical-secret.yaml` (`projectSlug`), and run
   `infisical init` here once so `make uploads-to-r2` knows the project.
4. **Image**: push to `main`. When CI has run, make the `easydocs` package public (GitHub → your
   profile → Packages → easydocs → Package settings) so the cluster can pull it without credentials.
5. **Cluster**: `kubectl apply -f deploy/argocd/easydocs.yaml`. Wait for both pods, then check
   `curl https://easydocs-api.gavinf.com/readyz`.

### Moving over from the VM

The VM still serves `easydocs.gavinf.com` until this, so do it in one go:

1. `make uploads-to-r2` copies every uploaded file into R2, keeping its URL (run it with
   `--dry-run` first to see the list). From here the VM can't show images, so carry on.
2. In Cloudflare, delete the `easydocs.gavinf.com` DNS record (the old tunnel's route). A Worker's
   custom domain can't be added while a record has that name.
3. **Workers Builds**: Workers & Pages → Create → Import a repository → `gavinfancher/easydocs`,
   root directory `web`, build command `npm run build`, deploy command `npx wrangler deploy`. The
   first deploy adds the `easydocs.gavinf.com` custom domain.
4. Open `https://easydocs.gavinf.com`, sign in, and check a doc with an image.
5. Stop the VM (`ssh ubuntu@easydocs.ad.gavinf.com 'cd /opt/easydocs && docker compose -f deploy/compose.yml down'`).
   Once you're happy, delete VM 506, the old `easydocs` tunnel, `deploy/compose.yml`,
   `deploy/cloud-init.yaml` and the `vm-*` Makefile targets.

### Local dev

```sh
make dev           # http://localhost:8000 (EASYDOCS_PORT=8010 make dev if 8000 is taken)
make test          # starts a throwaway postgres:18.4 on 127.0.0.1:5433 the first time
```

`make dev` runs the app with its own Postgres container and no login. Without R2 settings, uploads are kept in Postgres.

## Writing docs

- **New doc**: click **New doc** and give it a title and a folder.
- **Home page**: in the admin panel's **Home page** tab, set the heading, tagline and an
  optional markdown intro, and choose whether to show folder cards and recently updated docs.
- **Organize**: in the admin panel, drag docs onto folders, or use a row's **⋯** menu to rename, move or delete it.
- **Images**: paste a screenshot, drag and drop a file, or click **Image**. It's uploaded
  and the markdown link is inserted for you. Images use absolute `/uploads/...` URLs,
  so renaming or moving docs never breaks them.
- **Resize images**: right-click an image in the editor's preview and pick a size, or choose
  **Custom size…** and drag its corner. This writes the size into the markdown as `![alt|50%](/uploads/...)`
  (or `|300` for pixels), so it looks the same on the site.
- **Reuse uploads**: **From uploads** inserts something you've already uploaded. The admin
  panel's **Uploads** tab shows every file and which docs use it.
- **Upload folders**: in the Uploads tab (or the **From uploads** picker), drag files onto a
  folder in the left-hand list, or right-click for more. Folders only organize; a file's URL
  never changes, so filing it away can't break a doc.
- **Diagrams**: write a ` ```mermaid ` block and it renders as a diagram. Exported PNG/SVG
  diagrams from Excalidraw, draw.io, etc. work like any image.
- **Right-click** almost anything: docs and folders in the sidebar, uploads, images in the preview,
  and the text itself (cut/copy/paste, **Format**, **Paragraph** and **Insert** menus).
  Shift+right-click gives you the browser's own menu, e.g. for spelling suggestions.
- **Shortcuts**: ⌘/Ctrl+S save, ⌘B bold, ⌘⇧I italic, ⌘E inline code, ⌘I code block, ⌘K link, ⌘⌥1–3 headings,
  ⌘⇧8 bullets, ⌘⇧7 numbers, ⌘⇧9 checklist, ⌘⇧. quote.
- **Sidebar**: the button at the top left shows or hides it.

## Limits

- A doc can be up to 350 KB of markdown. Put big content in uploads.
- Uploads can be up to 25 MB.
- Search and the uploads "used in" list read every doc. That's fine for hundreds of docs; past that,
  Postgres full-text search is the next step.
