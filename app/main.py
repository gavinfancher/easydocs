"""mydocs — a small self-hosted markdown docs site.

Docs, folders, settings and uploaded files all live in Postgres (see store.py).
Uploads are referenced by absolute URLs (/uploads/<name>), so moving a doc never breaks
its images. Upload folders are only labels, so filing an upload never changes its URL.

Reading is public. Everything under /admin needs the admin password (see auth.py), except
with MYDOCS_LOCAL=1, for local dev (see compose.yml).
"""

import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import auth, store

SITE_TITLE = os.environ.get("MYDOCS_TITLE", "Docs")
LOCAL = os.environ.get("MYDOCS_LOCAL") == "1"
LOGIN_REQUIRED = not LOCAL
if LOGIN_REQUIRED and not auth.PASSWORD_HASH:
    raise RuntimeError("Set MYDOCS_ADMIN_PASSWORD_HASH (make one with: uv run python -m app.auth)")

MAX_DOC_BYTES = 350_000
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
CONTENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml", ".avif": "image/avif",
    ".pdf": "application/pdf", ".mp4": "video/mp4", ".webm": "video/webm",
    ".zip": "application/zip", ".txt": "text/plain", ".csv": "text/csv",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif"}

HOME_DEFAULTS = {
    "heading": SITE_TITLE,
    "tagline": "Guides, notes and reference — all in one place.",
    "body": "",
    "show_sections": True,
    "show_recent": True,
}

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

@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_schema()
    if not store.any_with_prefix("DOC", ""):
        store.put(doc_item("index", WELCOME))
    yield


app = FastAPI(title="mydocs", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


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


def same_origin(request: Request) -> bool:
    """Was this sent by our own pages? Browsers always say where a POST/PUT/DELETE came from."""
    source = request.headers.get("origin") or request.headers.get("referer") or ""
    return urlsplit(source).netloc == request.headers.get("host")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if LOGIN_REQUIRED and (path == "/admin" or path.startswith("/admin/")) and path != "/admin/login":
        if not await run_in_threadpool(auth.valid_session, request.cookies.get(auth.COOKIE)):
            if path.startswith("/admin/api/"):
                return JSONResponse({"detail": "Sign in first"}, status_code=401)
            return RedirectResponse("/admin/login?next=" + quote(path), status_code=303)
        # The cookie is SameSite=Strict already; this is a second lock against cross-site requests.
        if request.method not in ("GET", "HEAD") and not same_origin(request):
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
    return await call_next(request)


# ---------- path helpers ----------

def clean_rel(rel: str) -> str:
    rel = rel.strip().strip("/").removesuffix(".md")
    parts = [p.strip() for p in rel.split("/") if p.strip()]
    if not parts or any(p in (".", "..") or p.startswith(".") for p in parts):
        raise HTTPException(400, "Invalid path")
    return "/".join(parts)


def parents(path: str) -> list[str]:
    """'a/b/c' -> ['a', 'a/b']"""
    parts = path.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def title_of(path: str, content: str) -> str:
    for line in content.splitlines():
        m = re.match(r"#\s+(.+)", line)
        if m:
            return m.group(1).strip()
    return path.rpartition("/")[2].replace("-", " ").replace("_", " ").strip().capitalize()


def doc_item(path: str, content: str, updated: int | None = None) -> dict:
    if len(content.encode("utf-8")) > MAX_DOC_BYTES:
        raise HTTPException(413, "Doc too large (max 350 KB) — split it up, or move big content into uploads")
    return {"pk": "DOC", "sk": path, "title": title_of(path, content), "content": content,
            "updated": updated or int(time.time())}


def ensure_folders(paths: list[str]) -> None:
    store.batch_write(puts=[{"pk": "FOLDER", "sk": p} for p in paths])


def build_tree(folders: list[str], docs: list[dict]) -> list[dict]:
    root: list[dict] = []
    children = {"": root}

    def children_of(path: str) -> list[dict]:
        if path not in children:
            parent, _, name = path.rpartition("/")
            node = {"type": "folder", "name": name, "path": path, "children": []}
            children_of(parent).append(node)
            children[path] = node["children"]
        return children[path]

    for f in folders:
        children_of(f)
    for d in docs:
        parent, _, name = d["sk"].rpartition("/")
        children_of(parent).append({"type": "doc", "name": name, "path": d["sk"], "title": d["title"]})
    for items in children.values():
        items.sort(key=lambda c: (c["type"] == "doc", c["name"].lower()))
    return root


# ---------- public API ----------

@app.get("/api/config")
def config():
    return {"title": SITE_TITLE}


@app.get("/api/tree")
def tree():
    folders = [f["sk"] for f in store.query("FOLDER", fields=["sk"])]
    return build_tree(folders, store.query("DOC", fields=["sk", "title"]))


@app.get("/api/docs/{rel:path}")
def get_doc(rel: str):
    doc = store.get("DOC", clean_rel(rel))
    if not doc:
        raise HTTPException(404, "Doc not found")
    return {"path": doc["sk"], "title": doc["title"], "content": doc["content"]}


def load_home() -> dict:
    saved = store.get("SITE", "HOME") or {}
    return {k: saved.get(k, v) for k, v in HOME_DEFAULTS.items()}


@app.get("/api/home")
def home():
    """Landing page settings plus the most recently edited docs."""
    docs = sorted(store.query("DOC", fields=["sk", "title", "updated"]), key=lambda d: d["updated"], reverse=True)
    recent = [{"path": d["sk"], "title": d["title"], "modified": int(d["updated"])} for d in docs[:6]]
    return {**load_home(), "recent": recent}


@app.get("/api/search")
def search(q: str):
    q = q.strip().lower()
    if not q:
        return []
    results = []
    for doc in store.query("DOC", fields=["sk", "title", "content"]):
        text, title = doc["content"], doc["title"]
        idx = text.lower().find(q)
        if idx == -1 and q not in title.lower():
            continue
        snippet = ""
        if idx != -1:
            start = max(0, idx - 60)
            snippet = ("…" if start else "") + text[start: idx + 100].replace("\n", " ") + "…"
        results.append({"path": doc["sk"], "title": title, "snippet": snippet})
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
    path = clean_rel(body.path)
    try:
        store.put(doc_item(path, body.content), new=True)
    except store.Exists:
        raise HTTPException(409, "A doc with that path already exists")
    ensure_folders(parents(path))
    return {"path": path}


@app.put("/admin/api/docs/{rel:path}")
def save_doc(rel: str, body: DocBody):
    path = clean_rel(rel)
    item = doc_item(path, body.content)
    store.put(item)
    ensure_folders(parents(path))
    return {"path": path, "title": item["title"]}


@app.delete("/admin/api/docs/{rel:path}")
def delete_doc(rel: str):
    try:
        store.delete("DOC", clean_rel(rel))
    except store.Missing:
        raise HTTPException(404, "Doc not found")
    return {"ok": True}


@app.post("/admin/api/move")
def move(body: MoveBody):
    """Rename/move a doc or a folder."""
    src, dst = clean_rel(body.src), clean_rel(body.dst)
    doc = store.get("DOC", src)
    if doc:
        try:
            store.put(doc_item(dst, doc["content"], doc["updated"]), new=True)
        except store.Exists:
            raise HTTPException(409, "Something already exists at that path")
        store.delete("DOC", src)
        ensure_folders(parents(dst))
        return {"path": dst}

    if not store.get("FOLDER", src):
        raise HTTPException(404, "Not found")
    if dst.startswith(src + "/"):
        raise HTTPException(400, "Can't move a folder into itself")
    if store.get("FOLDER", dst) or store.any_with_prefix("DOC", dst + "/"):
        raise HTTPException(409, "Something already exists at that path")
    docs = store.query("DOC", src + "/")
    folders = [src] + [f["sk"] for f in store.query("FOLDER", src + "/", fields=["sk"])]
    moved = lambda p: dst + p[len(src):]
    # Write the new copies before deleting the old ones, so a failure halfway loses nothing.
    store.batch_write(puts=[{**d, "sk": moved(d["sk"])} for d in docs]
                      + [{"pk": "FOLDER", "sk": moved(f)} for f in folders])
    ensure_folders(parents(dst))
    store.batch_write(deletes=[("DOC", d["sk"]) for d in docs] + [("FOLDER", f) for f in folders])
    return {"path": dst}


@app.post("/admin/api/folders")
def create_folder(body: FolderBody):
    path = clean_rel(body.path)
    ensure_folders(parents(path) + [path])
    return {"path": path}


@app.delete("/admin/api/folders/{rel:path}")
def delete_folder(rel: str):
    path = clean_rel(rel)
    if not store.get("FOLDER", path):
        raise HTTPException(404, "Folder not found")
    if store.any_with_prefix("DOC", path + "/") or store.any_with_prefix("FOLDER", path + "/"):
        raise HTTPException(409, "Folder isn't empty — move or delete its docs first")
    store.delete("FOLDER", path)
    return {"ok": True}


@app.put("/admin/api/home")
def save_home(body: HomeBody):
    store.put({"pk": "SITE", "sk": "HOME", **body.model_dump()})
    return load_home()


# ---------- upload folders ----------
# Folders for uploads are only labels (UPLOADFOLDER items, plus a folder on each filed UPLOAD
# item). Files stay flat in the bucket, so filing one away never changes its URL.

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
    files = {f["sk"]: f["folder"] for f in store.query("UPLOAD") if f.get("folder")}
    folders = {f["sk"] for f in store.query("UPLOADFOLDER", fields=["sk"])}
    for f in [*folders, *files.values()]:
        folders |= with_parents(f)
    return {"folders": folders, "files": files}


def save_upload_folders(meta: dict) -> None:
    """Write only what changed since the last load."""
    old = load_upload_folders()
    folders = meta["folders"] - {""}
    files = {k: v for k, v in meta["files"].items() if v}  # '' = unsorted, no item needed
    store.batch_write(
        puts=[{"pk": "UPLOADFOLDER", "sk": f} for f in folders - old["folders"]]
        + [{"pk": "UPLOAD", "sk": k, "folder": v} for k, v in files.items() if old["files"].get(k) != v],
        deletes=[("UPLOADFOLDER", f) for f in old["folders"] - folders]
        + [("UPLOAD", k) for k in old["files"].keys() - files.keys()],
    )


def check_upload(name: str) -> str:
    if not re.fullmatch(r"[\w.-]+", name) or name.startswith(".") or not store.upload_exists(name):
        raise HTTPException(404, "File not found")
    return name


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
        check_upload(name)
        if folder:
            meta["files"][name] = folder
        else:
            meta["files"].pop(name, None)
    meta["folders"] |= with_parents(folder)
    save_upload_folders(meta)
    return {"ok": True, "folder": folder}


@app.post("/admin/api/uploads")
def upload(file: UploadFile = File(...), folder: str = Form("")):
    name = PurePosixPath(file.filename or "file").name
    ext = PurePosixPath(name).suffix.lower()
    content_type = file.content_type or ""
    if not ext and content_type.startswith("image/"):
        ext = "." + content_type.split("/")[1].split("+")[0]
    if ext not in CONTENT_TYPES:
        raise HTTPException(400, f"File type {ext or '(none)'} not allowed")
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large (max 25 MB)")
    folder = clean_upload_folder(folder)
    stem = PurePosixPath(name).stem
    final = f"{file_slug(stem)}-{uuid.uuid4().hex[:8]}{ext}"
    store.put_upload(final, data, CONTENT_TYPES[ext])
    if folder:
        meta = load_upload_folders()
        meta["files"][final] = folder
        meta["folders"] |= with_parents(folder)
        save_upload_folders(meta)
    return {"url": f"/uploads/{final}", "name": stem, "is_image": ext in IMAGE_EXTS, "folder": folder}


@app.get("/admin/api/uploads")
def list_uploads():
    """Upload folders, plus every uploaded file (newest first) with its folder and the docs that use it."""
    docs = {d["sk"]: d["content"] for d in store.query("DOC", fields=["sk", "content"])}
    home_body = load_home()["body"]
    files = sorted(store.list_uploads(), key=lambda f: f["modified"], reverse=True)
    meta = load_upload_folders()
    return {"folders": sorted(meta["folders"], key=str.lower), "files": [{
        **f,
        "folder": meta["files"].get(f["name"], ""),
        "url": f"/uploads/{f['name']}",
        "is_image": PurePosixPath(f["name"]).suffix.lower() in IMAGE_EXTS,
        "used_in": sorted(path for path, text in docs.items() if f"/uploads/{f['name']}" in text),
        "on_home": f"/uploads/{f['name']}" in home_body,
    } for f in files]}


class RenameUploadBody(BaseModel):
    name: str


def file_slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", name).strip("-").lower()[:40] or "file"


@app.post("/admin/api/uploads/{name}/rename")
def rename_upload(name: str, body: RenameUploadBody):
    """Rename an uploaded file and update every doc that links to it."""
    f = PurePosixPath(check_upload(name))
    m = re.match(r"^(.*?)(?:-([0-9a-f]{8}))?$", f.stem)
    old_stem, tag = m.group(1), m.group(2) or uuid.uuid4().hex[:8]
    new_stem = file_slug(PurePosixPath(body.name.strip()).stem if body.name.strip().lower().endswith(f.suffix.lower()) else body.name.strip())
    final = f"{new_stem}-{tag}{f.suffix}"
    if final == name:
        return {"name": final, "url": f"/uploads/{final}", "docs": []}
    if store.upload_exists(final):
        raise HTTPException(409, "A file with that name already exists")
    store.rename_upload(name, final)
    meta = load_upload_folders()
    if name in meta["files"]:
        meta["files"][final] = meta["files"].pop(name)
        save_upload_folders(meta)
    old_url, new_url = f"/uploads/{name}", f"/uploads/{final}"
    # Also swap the alt/link text if it's still the old auto-generated name.
    # (An image's alt text may end in a size, like "diagram|50%"; that part is kept.)
    alt_re = re.compile(r"(!?\[)" + re.escape(old_stem) + r"((?:\s*\|[^\]]*)?\]\(" + re.escape(old_url) + r"\))")
    relink = lambda text: alt_re.sub(lambda m: m.group(1) + new_stem + m.group(2), text).replace(old_url, new_url)
    changed = []
    for doc in store.query("DOC"):
        if old_url in doc["content"]:
            store.put(doc_item(doc["sk"], relink(doc["content"])))
            changed.append(doc["sk"])
    settings = load_home()
    if old_url in settings["body"]:
        store.put({"pk": "SITE", "sk": "HOME", **settings, "body": relink(settings["body"])})
    return {"name": final, "url": new_url, "old_stem": old_stem, "new_stem": new_stem, "docs": sorted(changed)}


@app.delete("/admin/api/uploads/{name}")
def delete_upload(name: str):
    store.delete_upload(check_upload(name))
    meta = load_upload_folders()
    if meta["files"].pop(name, None) is not None:
        save_upload_folders(meta)
    return {"ok": True}


# ---------- sign in / out ----------

@app.post("/admin/login")
def login(request: Request, password: str = Form(""), next: str = Form("/admin/")):
    # Only ever send people on to an admin page, never off-site.
    next = next if next.startswith("/admin") and not next.startswith("/admin/login") else "/admin/"
    ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "")
    if not same_origin(request):
        raise HTTPException(403, "Cross-site request refused")
    if auth.throttled(ip):
        return RedirectResponse("/admin/login?error=throttled&next=" + quote(next), status_code=303)
    if not auth.check_password(password):
        auth.record_failure(ip)
        return RedirectResponse("/admin/login?error=wrong&next=" + quote(next), status_code=303)
    response = RedirectResponse(next, status_code=303)
    response.set_cookie(auth.COOKIE, auth.start_session(), max_age=auth.SESSION_DAYS * 86400, path="/",
                        httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return response


@app.post("/admin/logout")
def logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(auth.COOKIE, path="/")
    return response


@app.get("/api/session")
def session(request: Request):
    """Whether this visitor can use the admin pages (the pages use it to show Sign out)."""
    return {"login_required": LOGIN_REQUIRED,
            "signed_in": not LOGIN_REQUIRED or auth.valid_session(request.cookies.get(auth.COOKIE))}


# ---------- pages, static files and uploads ----------

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/uploads/{name}")
def get_upload(name: str):
    try:
        data, content_type = store.get_upload(check_upload(name))
    except store.Missing:
        raise HTTPException(404, "File not found")
    # Uploaded SVGs could carry scripts; sandbox anything opened directly.
    headers = {"Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox",
               "X-Content-Type-Options": "nosniff",
               "Cache-Control": "public, max-age=31536000, immutable"}
    return Response(data, media_type=content_type, headers=headers)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/admin")
def admin_redirect():
    return RedirectResponse("/admin/")


@app.get("/admin/login")
def login_page():
    return FileResponse(os.path.join(STATIC_DIR, "login.html"))


@app.get("/admin/edit")
@app.get("/admin/edit/{rel:path}")
def editor_page(rel: str = ""):
    # /admin/edit/<doc path> opens that doc; the page reads the path itself.
    return FileResponse(os.path.join(STATIC_DIR, "editor.html"))


@app.get("/admin/{tab:path}")
def admin_page(tab: str = ""):
    return FileResponse(os.path.join(STATIC_DIR, "admin.html"))


@app.get("/")
@app.get("/docs/{rel:path}")
def viewer_page(rel: str = ""):
    return FileResponse(os.path.join(STATIC_DIR, "viewer.html"))
