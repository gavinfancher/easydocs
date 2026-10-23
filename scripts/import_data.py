"""Copy a data/ folder from the old file-based mydocs into Postgres.

    MYDOCS_DATABASE_URL=postgresql://... uv run python scripts/import_data.py data

(or `make import DB=postgresql://...`). Safe to run again: existing items are overwritten.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MYDOCS_LOCAL", "1")  # only borrowing helpers from main; no server runs here

from app import store  # noqa: E402
from app.main import CONTENT_TYPES, HOME_DEFAULTS, doc_item, parents  # noqa: E402


def main(data: Path) -> None:
    store.init_schema()
    docs_dir, uploads_dir = data / "docs", data / "uploads"
    hidden = lambda p, root: any(part.startswith(".") for part in p.relative_to(root).parts)

    docs, folders = [], set()
    for f in sorted(docs_dir.rglob("*.md")):
        if hidden(f, docs_dir):
            continue
        path = f.relative_to(docs_dir).as_posix()[:-3]
        docs.append(doc_item(path, f.read_text(encoding="utf-8"), int(f.stat().st_mtime)))
        folders.update(parents(path))
    for d in docs_dir.rglob("*"):
        if d.is_dir() and not hidden(d, docs_dir):
            folders.add(d.relative_to(docs_dir).as_posix())
    store.batch_write(puts=docs + [{"pk": "FOLDER", "sk": f} for f in sorted(folders)])
    print(f"{len(docs)} docs, {len(folders)} folders")

    if (data / "home.json").is_file():
        saved = json.loads((data / "home.json").read_text(encoding="utf-8"))
        store.put({"pk": "SITE", "sk": "HOME", **{k: saved.get(k, v) for k, v in HOME_DEFAULTS.items()}})
        print("home page settings")

    count = 0
    for f in sorted(uploads_dir.iterdir()) if uploads_dir.is_dir() else []:
        if f.is_file() and not f.name.startswith("."):
            store.put_upload(f.name, f.read_bytes(), CONTENT_TYPES.get(f.suffix.lower(), "application/octet-stream"))
            count += 1
    print(f"{count} uploads")

    meta_file = data / "upload-folders.json"
    if meta_file.is_file():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        store.batch_write(puts=[{"pk": "UPLOADFOLDER", "sk": f} for f in meta.get("folders", [])]
                          + [{"pk": "UPLOAD", "sk": k, "folder": v} for k, v in meta.get("files", {}).items() if v])
        print("upload folders")


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "data"))
