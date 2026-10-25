"""Move uploaded files' bytes out of Postgres into R2, keeping their names (so every /uploads/ URL
in the docs keeps working). Safe to re-run: it only moves files that are still in Postgres, and it
clears a file's bytes from Postgres only after R2 confirms it has them.

    infisical run --env=prod --path=/ -- uv run python scripts/uploads_to_r2.py           # do it
    infisical run --env=prod --path=/ -- uv run python scripts/uploads_to_r2.py --dry-run # just list

Needs EASYDOCS_DATABASE_URL and the four EASYDOCS_R2_* variables.
"""

import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import storage, store  # noqa: E402


def main(dry_run: bool) -> None:
    if not storage.enabled():
        raise SystemExit("Set EASYDOCS_R2_ACCOUNT_ID, _BUCKET, _ACCESS_KEY_ID and _SECRET_ACCESS_KEY first.")
    names = store.uploads_in_postgres()
    print(f"{len(names)} file(s) in Postgres to move to bucket {storage.BUCKET}")
    for name in names:
        row = store.get_upload(name)
        if dry_run:
            print(f"  would move {name} ({row['size']:,} bytes)")
            continue
        key = storage.new_key(PurePosixPath(name).suffix.lower())
        etag = storage.put(key, store.get_upload_data(name), row["content_type"])
        arrived = storage.head(key)
        if not arrived or arrived["size"] != row["size"]:
            raise SystemExit(f"{name}: R2 doesn't have all of it ({arrived}); stopped, nothing cleared")
        store.moved_to_r2(name, storage.BUCKET, key, etag)
        print(f"  moved {name} -> {key}")


if __name__ == "__main__":
    main("--dry-run" in sys.argv[1:])
