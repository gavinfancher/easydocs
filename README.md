# mydocs

A tiny self-hosted markdown docs site with a web editor.

- **`/`**: read-only docs site: a home page, then sidebar, search and diagrams
- **`/admin/`**: admin panel. Organize docs and folders, and manage uploads
- **`/admin/edit`**: the editor, with live preview

## Run it

**Docker (recommended for self-hosting)**

```sh
docker compose up -d --build
```

Open http://localhost:8000. Your docs and uploads live in `./data`.

**Locally with uv**

```sh
uv run uvicorn app.main:app --reload
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

## Configuration

| Env var           | Default | Purpose                         |
| ----------------- | ------- | ------------------------------- |
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

- There's no login: anyone who can reach the server can read **and edit** everything. The compose file only
  listens on this machine (`127.0.0.1`); put a reverse proxy with auth (or a VPN) in front before exposing it.
