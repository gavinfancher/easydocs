# mydocs

A tiny self-hosted markdown docs site with a web editor.

- **`/`**: read-only docs site: a home page, then sidebar, search and diagrams
- **`/admin/`**: admin panel (log in here). Organize docs and folders, and manage uploads
- **`/admin/edit`**: the editor, with live preview

## Run it

**Docker (recommended for self-hosting)**

```sh
cp .env.example .env        # then set a real MYDOCS_PASSWORD
docker compose up -d --build
```

Open http://localhost:8000. Your docs and uploads live in `./data`.

**Locally with uv**

```sh
MYDOCS_PASSWORD=change-me uv run uvicorn app.main:app --reload
```

Open http://localhost:8000.

## Writing docs

- **New doc**: click **New doc** and give it a title and a folder.
- **Home page**: in the admin panel's **Home page** tab, set the heading, tagline and an
  optional markdown intro, and choose whether to show folder cards and recently updated docs.
- **Organize**: in the admin panel, drag docs onto folders, or use a row's **⋯** menu to rename, move or delete it.
- **Images**: paste a screenshot, drag and drop a file, or click **Image**. It's uploaded
  and the markdown link is inserted for you. Images use absolute `/uploads/...` URLs,
  so renaming or moving docs never breaks them.
- **Resize images**: click an image in the editor's preview, then pick 25% / 50% / 75% / Full
  or drag its corner. This writes the size into the markdown as `![alt|50%](/uploads/...)`
  (or `|300` for pixels), so it looks the same on the site.
- **Reuse uploads**: **From uploads** inserts something you've already uploaded. The admin
  panel's **Uploads** tab shows every file and which docs use it.
- **Upload folders**: in the Uploads tab (or the **From uploads** picker), drag files onto a
  folder in the left-hand list, or right-click for more. Folders only organize; a file's URL
  never changes, so filing it away can't break a doc.
- **Diagrams**: write a ` ```mermaid ` block and it renders as a diagram. Exported PNG/SVG
  diagrams from Excalidraw, draw.io, etc. work like any image.
- **Shortcuts**: ⌘/Ctrl+S save, ⌘B bold, ⌘I italic, ⌘K link.
- **Sidebar**: the button at the top left shows or hides it.

## Configuration

| Env var           | Default | Purpose                         |
| ----------------- | ------- | ------------------------------- |
| `MYDOCS_PASSWORD` | `admin` | Admin password — **change it**  |
| `MYDOCS_USER`     | `admin` | Admin username                  |
| `MYDOCS_TITLE`    | `Docs`  | Site name in the header         |
| `MYDOCS_DATA`     | `data`  | Where docs and uploads are kept |

## Storage

Everything is plain files, so back up `data/` and you've backed up everything:

```
data/
  docs/       # your .md files, folders = sidebar sections
  uploads/    # pasted/dropped images and files
  upload-folders.json   # which upload is in which folder
  home.json             # home page settings
```

You can also edit the `.md` files directly; changes show up on refresh.

## Notes

- The viewer is public. Put it behind your reverse proxy's auth (or a VPN) if the docs are private.
- Admin login uses a 30-day session cookie; changing the password logs everyone out. Serve it over HTTPS (e.g. Caddy or Traefik in front).
