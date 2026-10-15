"""mydocs — a tiny self-hosted markdown docs site.

Docs are plain .md files under DATA/docs (folders = sections).
Uploaded images/files live under DATA/uploads and are referenced by absolute
URLs (/uploads/<name>), so moving a doc never breaks its images. Upload folders
are labels in DATA/upload-folders.json, so filing an upload never changes its URL.
"""

import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

log = logging.getLogger("uvicorn.error")

DATA_DIR = Path(os.environ.get("MYDOCS_DATA", "data")).resolve()
DOCS_DIR = DATA_DIR / "docs"
UPLOADS_DIR = DATA_DIR / "uploads"
STATIC_DIR = Path(__file__).parent / "static"

SITE_TITLE = os.environ.get("MYDOCS_TITLE", "Docs")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}
ALLOWED_UPLOADS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif",
    ".pdf", ".mp4", ".webm", ".zip", ".txt", ".csv",
}

DOCS_DIR.mkdir(parents=True, exist_ok=True)
HOME_FILE = DATA_DIR / "home.json"
HOME_DEFAULTS = {
    "heading": SITE_TITLE,
    "tagline": "Guides, notes and reference — all in one place.",
    "body": "",
    "show_sections": True,
    "show_recent": True,
}
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

WELCOME = """# Welcome

This is your docs site. Open the **[admin editor](/admin/)** to write docs.

## Images

In the editor, just **paste** a screenshot, **drag & drop** an image, or click the
image button. It's uploaded and linked for you — no paths to think about.

## Diagrams

Write a `mermaid` code block and it renders as a diagram:

```mermaid
flowchart LR
    Write[Write in /admin] --> Save --> Read[Read on the site]
```

## Organizing

Use folders like `guides/setup` when creating a doc. Folders show up as
sections in the sidebar. The first `# Heading` in a doc is its title.
"""

if not any(DOCS_DIR.iterdir()):
    (DOCS_DIR / "index.md").write_text(WELCOME)

app = FastAPI(title="mydocs", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/admin"):
        # Never cache admin pages or API responses.
        response.headers["Cache-Control"] = "no-store"
    elif not path.startswith("/uploads/"):
        # Pages, API and app code: always check for a newer version (cheap via ETag).
        response.headers.setdefault("Cache-Control", "no-cache")
    return response

# ---------- no login: block cross-site writes ----------

@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    # There's no login, so stop other websites' pages from changing things via the
    # visitor's browser: a write from a page must come from this site.
    origin = request.headers.get("origin")
    if (request.method not in ("GET", "HEAD", "OPTIONS") and origin
            and origin.split("://", 1)[-1] != request.headers.get("host")):
        return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
    return await call_next(request)


# ---------- path helpers ----------

def clean_rel(rel: str) -> str:
    rel = rel.strip().strip("/").removesuffix(".md")
    parts = [p.strip() for p in rel.split("/") if p.strip()]
    if not parts or any(p in (".", "..") or p.startswith(".") for p in parts):
        raise HTTPException(400, "Invalid path")
    return "/".join(parts)


def safe_join(rel: str) -> Path:
    p = (DOCS_DIR / rel).resolve()
    if DOCS_DIR not in p.parents:
        raise HTTPException(400, "Invalid path")
    return p


def doc_file(rel: str) -> Path:
    return safe_join(clean_rel(rel) + ".md")


def folder_dir(rel: str) -> Path:
    return safe_join(clean_rel(rel))


def title_of(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                m = re.match(r"#\s+(.+)", line)
                if m:
                    return m.group(1).strip()
    except OSError:
        pass
    return path.stem.replace("-", " ").replace("_", " ").strip().capitalize()


def build_tree(d: Path) -> list[dict]:
    items = []
    for child in sorted(d.iterdir(), key=lambda c: (c.is_file(), c.name.lower())):
        if child.name.startswith("."):
            continue
        rel = child.relative_to(DOCS_DIR).as_posix()
        if child.is_dir():
            items.append({"type": "folder", "name": child.name, "path": rel, "children": build_tree(child)})
        elif child.suffix == ".md":
            items.append({"type": "doc", "name": child.stem, "path": rel[:-3], "title": title_of(child)})
    return items


# ---------- public API ----------

@app.get("/api/config")
def config():
    return {"title": SITE_TITLE}


@app.get("/api/tree")
def tree():
    return build_tree(DOCS_DIR)


@app.get("/api/docs/{rel:path}")
def get_doc(rel: str):
    f = doc_file(rel)
    if not f.is_file():
        raise HTTPException(404, "Doc not found")
    return {"path": clean_rel(rel), "title": title_of(f), "content": f.read_text(encoding="utf-8")}


def load_home() -> dict:
    try:
        saved = json.loads(HOME_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        saved = {}
    return {k: saved.get(k, v) for k, v in HOME_DEFAULTS.items()}


@app.get("/api/home")
def home():
    """Landing page settings plus the most recently edited docs."""
    files = sorted((f for f in DOCS_DIR.rglob("*.md")
                    if not any(p.startswith(".") for p in f.relative_to(DOCS_DIR).parts)),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    recent = [{"path": f.relative_to(DOCS_DIR).as_posix()[:-3], "title": title_of(f),
               "modified": int(f.stat().st_mtime)} for f in files[:6]]
    return {**load_home(), "recent": recent}


@app.get("/api/search")
def search(q: str):
    q = q.strip().lower()
    if not q:
        return []
    results = []
    for f in sorted(DOCS_DIR.rglob("*.md")):
        if any(part.startswith(".") for part in f.relative_to(DOCS_DIR).parts):
            continue
        text = f.read_text(encoding="utf-8")
        title = title_of(f)
        idx = text.lower().find(q)
        if idx == -1 and q not in title.lower():
            continue
        snippet = ""
        if idx != -1:
            start = max(0, idx - 60)
            snippet = ("…" if start else "") + text[start: idx + 100].replace("\n", " ") + "…"
        results.append({"path": f.relative_to(DOCS_DIR).as_posix()[:-3], "title": title, "snippet": snippet})
    return results[:50]


# ---------- admin API ----------

class DocBody(BaseModel):
    content: str


class CreateBody(BaseModel):
    path: str
    content: str = ""


class MoveBody(BaseModel):
    src: str
    dst: str


class FolderBody(BaseModel):
    path: str


class HomeBody(BaseModel):
    heading: str = ""
    tagline: str = ""
    body: str = ""
    show_sections: bool = True
    show_recent: bool = True


@app.post("/admin/api/docs")
def create_doc(body: CreateBody):
    f = doc_file(body.path)
    if f.exists():
        raise HTTPException(409, "A doc with that path already exists")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body.content, encoding="utf-8")
    return {"path": clean_rel(body.path)}


@app.put("/admin/api/docs/{rel:path}")
def save_doc(rel: str, body: DocBody):
    f = doc_file(rel)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body.content, encoding="utf-8")
    return {"path": clean_rel(rel), "title": title_of(f)}


@app.delete("/admin/api/docs/{rel:path}")
def delete_doc(rel: str):
    f = doc_file(rel)
    if not f.is_file():
        raise HTTPException(404, "Doc not found")
    f.unlink()
    return {"ok": True}


@app.post("/admin/api/move")
def move(body: MoveBody):
    """Rename/move a doc or a folder."""
    src_doc, dst_doc = doc_file(body.src), doc_file(body.dst)
    if src_doc.is_file():
        src, dst = src_doc, dst_doc
    else:
        src, dst = folder_dir(body.src), folder_dir(body.dst)
        if not src.is_dir():
            raise HTTPException(404, "Not found")
        if src in dst.parents:
            raise HTTPException(400, "Can't move a folder into itself")
    if dst.exists():
        raise HTTPException(409, "Something already exists at that path")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dst)
    return {"path": clean_rel(body.dst)}


@app.post("/admin/api/folders")
def create_folder(body: FolderBody):
    folder_dir(body.path).mkdir(parents=True, exist_ok=True)
    return {"path": clean_rel(body.path)}


@app.delete("/admin/api/folders/{rel:path}")
def delete_folder(rel: str):
    d = folder_dir(rel)
    if not d.is_dir():
        raise HTTPException(404, "Folder not found")
    if any(d.iterdir()):
        raise HTTPException(409, "Folder isn't empty — move or delete its docs first")
    d.rmdir()
    return {"ok": True}


@app.put("/admin/api/home")
def save_home(body: HomeBody):
    HOME_FILE.write_text(json.dumps(body.model_dump(), indent=2), encoding="utf-8")
    return load_home()


# ---------- upload folders ----------
# Folders for uploads are only labels, kept in a small JSON file. Files stay flat in
# DATA/uploads, so filing one away never changes its URL or breaks a doc's link.
UPLOAD_FOLDERS_FILE = DATA_DIR / "upload-folders.json"


def clean_upload_folder(rel: str) -> str:
    """'' is the top level. Folder names are free text, minus slashes at the edges."""
    parts = [p.strip() for p in rel.strip().strip("/").split("/") if p.strip()]
    if any(len(p) > 80 or p in (".", "..") for p in parts):
        raise HTTPException(400, "Invalid folder name")
    return "/".join(parts)


def with_parents(folder: str) -> set[str]:
    parts = folder.split("/") if folder else []
    return {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}


def load_upload_folders() -> dict:
    try:
        data = json.loads(UPLOAD_FOLDERS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        data = {}
    files = {k: v for k, v in data.get("files", {}).items() if v and (UPLOADS_DIR / k).is_file()}
    folders = set(data.get("folders", []))
    for f in [*folders, *files.values()]:
        folders |= with_parents(f)
    return {"folders": folders, "files": files}


def save_upload_folders(meta: dict) -> None:
    files = {k: v for k, v in sorted(meta["files"].items()) if v}  # '' = unsorted, no entry needed
    out = {"folders": sorted(meta["folders"] - {""}, key=str.lower), "files": files}
    tmp = UPLOAD_FOLDERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2), encoding="utf-8")
    tmp.replace(UPLOAD_FOLDERS_FILE)


def upload_path(name: str) -> Path:
    f = (UPLOADS_DIR / name).resolve()
    if f.parent != UPLOADS_DIR or not f.is_file() or f.name.startswith("."):
        raise HTTPException(404, "File not found")
    return f


class UploadFolderBody(BaseModel):
    path: str


class MoveUploadFolderBody(BaseModel):
    src: str
    dst: str


class MoveUploadsBody(BaseModel):
    names: list[str]
    folder: str = ""


@app.post("/admin/api/upload-folders")
def create_upload_folder(body: UploadFolderBody):
    path = clean_upload_folder(body.path)
    if not path:
        raise HTTPException(400, "Give the folder a name")
    meta = load_upload_folders()
    if path in meta["folders"]:
        raise HTTPException(409, "That folder already exists")
    meta["folders"] |= with_parents(path)
    save_upload_folders(meta)
    return {"path": path}


def reparent(path: str, src: str, dst: str) -> str:
    """Swap the `src` prefix of a folder path for `dst`."""
    if path == src:
        return dst
    if path.startswith(src + "/"):
        return (dst + "/" if dst else "") + path[len(src) + 1:]
    return path


@app.post("/admin/api/upload-folders/move")
def move_upload_folder(body: MoveUploadFolderBody):
    """Rename or move a folder, along with everything inside it."""
    src, dst = clean_upload_folder(body.src), clean_upload_folder(body.dst)
    meta = load_upload_folders()
    if not src or src not in meta["folders"]:
        raise HTTPException(404, "Folder not found")
    if not dst:
        raise HTTPException(400, "Give the folder a name")
    if dst == src:
        return {"path": dst}
    if dst.startswith(src + "/"):
        raise HTTPException(400, "Can't move a folder into itself")
    if dst in meta["folders"]:
        raise HTTPException(409, "A folder with that name already exists there")
    meta["folders"] = {reparent(f, src, dst) for f in meta["folders"]} | with_parents(dst)
    meta["files"] = {k: reparent(v, src, dst) for k, v in meta["files"].items()}
    save_upload_folders(meta)
    return {"path": dst}


@app.post("/admin/api/upload-folders/delete")
def delete_upload_folder(body: UploadFolderBody):
    """Remove a folder. Nothing is deleted: what was inside moves up one level."""
    path = clean_upload_folder(body.path)
    meta = load_upload_folders()
    if not path or path not in meta["folders"]:
        raise HTTPException(404, "Folder not found")
    parent = path.rpartition("/")[0]
    meta["folders"] = {reparent(f, path, parent) for f in meta["folders"]} - {""}
    meta["files"] = {k: reparent(v, path, parent) for k, v in meta["files"].items()}
    save_upload_folders(meta)
    return {"ok": True}


@app.post("/admin/api/uploads/move")
def move_uploads(body: MoveUploadsBody):
    folder = clean_upload_folder(body.folder)
    meta = load_upload_folders()
    for name in body.names:
        upload_path(name)
        if folder:
            meta["files"][name] = folder
        else:
            meta["files"].pop(name, None)
    meta["folders"] |= with_parents(folder)
    save_upload_folders(meta)
    return {"ok": True, "folder": folder}


@app.post("/admin/api/uploads")
async def upload(file: UploadFile = File(...), folder: str = Form("")):
    name = Path(file.filename or "file").name
    ext = Path(name).suffix.lower()
    if not ext and file.content_type and file.content_type.startswith("image/"):
        ext = "." + file.content_type.split("/")[1].split("+")[0]
    if ext not in ALLOWED_UPLOADS:
        raise HTTPException(400, f"File type {ext or '(none)'} not allowed")
    final = f"{file_slug(Path(name).stem)}-{uuid.uuid4().hex[:8]}{ext}"
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (max 25 MB)")
    folder = clean_upload_folder(folder)
    (UPLOADS_DIR / final).write_bytes(data)
    if folder:
        meta = load_upload_folders()
        meta["files"][final] = folder
        meta["folders"] |= with_parents(folder)
        save_upload_folders(meta)
    return {"url": f"/uploads/{final}", "name": Path(name).stem, "is_image": ext in IMAGE_EXTS, "folder": folder}


@app.get("/admin/api/uploads")
def list_uploads():
    """Upload folders, plus every uploaded file (newest first) with its folder and the docs that use it."""
    docs = {f.relative_to(DOCS_DIR).as_posix()[:-3]: f.read_text(encoding="utf-8")
            for f in DOCS_DIR.rglob("*.md")}
    home_body = load_home()["body"]
    files = sorted((f for f in UPLOADS_DIR.iterdir() if f.is_file() and not f.name.startswith(".")),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    meta = load_upload_folders()
    return {"folders": sorted(meta["folders"], key=str.lower), "files": [{
        "name": f.name,
        "folder": meta["files"].get(f.name, ""),
        "url": f"/uploads/{f.name}",
        "size": f.stat().st_size,
        "modified": int(f.stat().st_mtime),
        "is_image": f.suffix.lower() in IMAGE_EXTS,
        "used_in": sorted(path for path, text in docs.items() if f"/uploads/{f.name}" in text),
        "on_home": f"/uploads/{f.name}" in home_body,
    } for f in files]}


class RenameUploadBody(BaseModel):
    name: str


def file_slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower()[:40] or "file"


@app.post("/admin/api/uploads/{name}/rename")
def rename_upload(name: str, body: RenameUploadBody):
    """Rename an uploaded file and update every doc that links to it."""
    f = upload_path(name)
    m = re.match(r"^(.*?)(?:-([0-9a-f]{8}))?$", f.stem)
    old_stem, tag = m.group(1), m.group(2) or uuid.uuid4().hex[:8]
    new_stem = file_slug(Path(body.name.strip()).stem if body.name.strip().lower().endswith(f.suffix.lower()) else body.name.strip())
    final = f"{new_stem}-{tag}{f.suffix}"
    if final == f.name:
        return {"name": final, "url": f"/uploads/{final}", "docs": []}
    if (UPLOADS_DIR / final).exists():
        raise HTTPException(409, "A file with that name already exists")
    meta = load_upload_folders()  # before the rename: loading drops entries for missing files
    f.rename(UPLOADS_DIR / final)
    if name in meta["files"]:
        meta["files"][final] = meta["files"].pop(name)
        save_upload_folders(meta)
    old_url, new_url = f"/uploads/{name}", f"/uploads/{final}"
    # Also swap the alt/link text if it's still the old auto-generated name.
    # (An image's alt text may end in a size, like "diagram|50%"; that part is kept.)
    alt_re = re.compile(r"(!?\[)" + re.escape(old_stem) + r"((?:\s*\|[^\]]*)?\]\(" + re.escape(old_url) + r"\))")
    changed = []
    for doc in DOCS_DIR.rglob("*.md"):
        text = doc.read_text(encoding="utf-8")
        if old_url not in text:
            continue
        text = alt_re.sub(lambda m: m.group(1) + new_stem + m.group(2), text).replace(old_url, new_url)
        doc.write_text(text, encoding="utf-8")
        changed.append(doc.relative_to(DOCS_DIR).as_posix()[:-3])
    settings = load_home()
    if old_url in settings["body"]:
        settings["body"] = alt_re.sub(lambda m: m.group(1) + new_stem + m.group(2), settings["body"]).replace(old_url, new_url)
        HOME_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return {"name": final, "url": new_url, "old_stem": old_stem, "new_stem": new_stem, "docs": sorted(changed)}


@app.delete("/admin/api/uploads/{name}")
def delete_upload(name: str):
    upload_path(name).unlink()
    meta = load_upload_folders()
    if meta["files"].pop(name, None) is not None:
        save_upload_folders(meta)
    return {"ok": True}


# ---------- files & pages ----------

@app.get("/uploads/{name}")
def get_upload(name: str):
    f = (UPLOADS_DIR / name).resolve()
    if f.parent != UPLOADS_DIR or not f.is_file():
        raise HTTPException(404)
    # Uploaded SVGs could carry scripts; sandbox anything opened directly.
    headers = {"Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox",
               "X-Content-Type-Options": "nosniff",
               "Cache-Control": "public, max-age=31536000, immutable"}
    return FileResponse(f, headers=headers)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/admin")
def admin_redirect():
    return RedirectResponse("/admin/")


@app.get("/admin/")
@app.get("/admin/uploads")
@app.get("/admin/home")
def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/admin/edit")
@app.get("/admin/edit/{rel:path}")
def editor_page(rel: str = ""):
    # /admin/edit/<doc path> opens that doc; the page reads the path itself.
    return FileResponse(STATIC_DIR / "editor.html")


@app.get("/")
@app.get("/docs/{rel:path}")
def viewer_page(rel: str = ""):
    return FileResponse(STATIC_DIR / "viewer.html")
