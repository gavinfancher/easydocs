# easydocs

A small self-hosted markdown docs site, with a web editor. Live at https://easydocs.gavinf.com.

- **`/`**: read-only docs site: a home page, then sidebar, search and diagrams
- **`/admin/`**: admin panel. Organize docs and folders, and manage uploads
- **`/admin/edit`**: the editor, with live preview

## How it runs

```
 browser ──► Cloudflare (easydocs.gavinf.com, HTTPS)
               └─ tunnel ──► cloudflared ──► easydocs app (FastAPI)  ── Proxmox VM 506, easydocs.ad.gavinf.com
                                                └──► Postgres, database "easydocs" ── postgres.ad.gavinf.com
```

The tunnel dials out from the VM, so nothing on the network is opened to the internet. Postgres holds
everything (docs, settings, uploaded files and sign-in sessions), so the VM keeps no state and can be
rebuilt from `deploy/cloud-init.yaml`.

Reading is public. The admin panel and editor need the admin password: you sign in at `/admin/login`
and get a session cookie (30 days, HttpOnly, Secure, SameSite=Strict) that signing out really ends.
Ten wrong passwords from one IP pause logins for 15 minutes.

| Piece | Where |
| --- | --- |
| API and pages (FastAPI) | `app/main.py` |
| Postgres access, table layout | `app/store.py` |
| Admin password and sessions | `app/auth.py`, `app/static/login.html` |
| Production containers (app + cloudflared) | `deploy/compose.yml` |
| VM setup | `deploy/cloud-init.yaml` |

### Deploy

```sh
make deploy        # rsync to the VM, rebuild and restart the containers
make logs          # tail the app and tunnel logs
```

Secrets live only on the VM, in `/opt/easydocs/deploy/.env` (mode 600): the database URL, the admin
password hash and the tunnel token. To change the admin password, run this in a terminal (it asks for
the new password twice, then updates the VM and restarts the app):

```sh
make set-password
```

The tunnel is `easydocs` in Cloudflare Zero Trust → Networks → Tunnels. Its public hostname
`easydocs.gavinf.com` points at `http://easydocs:8000`.

### Rebuilding the VM

On the Proxmox host (10.0.4.10), with `deploy/cloud-init.yaml` copied to
`/var/lib/vz/snippets/easydocs-user.yaml`:

```sh
qm create 506 --name easydocs --memory 2048 --cores 2 --cpu host --ostype l26 --agent enabled=1 --onboot 1 \
  --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-pci --serial0 socket --vga serial0
qm set 506 --scsi0 local-lvm:0,import-from=/var/lib/vz/template/iso/ubuntu-26.04-server-cloudimg-amd64.img
qm resize 506 scsi0 16G
qm set 506 --ide2 local-lvm:cloudinit --boot order=scsi0 --ipconfig0 ip=dhcp --cicustom user=local:snippets/easydocs-user.yaml
qm start 506
```

Then write `/opt/easydocs/deploy/.env` (see the top of `deploy/compose.yml`) and run `make deploy HOST=ubuntu@<ip>`.

### Local dev

```sh
make dev           # http://localhost:8000 (EASYDOCS_PORT=8010 make dev if 8000 is taken)
make test          # starts a throwaway postgres:18.4 on 127.0.0.1:5433 the first time
```

`make dev` runs the app with its own Postgres container and no login.

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
