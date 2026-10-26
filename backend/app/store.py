"""Storage: Postgres holds everything, docs and settings in `items`, uploaded files in `uploads`.

`items` is keyed by pk (what kind of thing) + sk (its path or name), with the rest in a jsonb column:

  pk            sk              data
  DOC           <doc path>      title, content, updated
  FOLDER        <folder path>   (every doc folder, so empty folders stick around)
  SITE          HOME            home page settings
  UPLOAD        <file name>     folder (only for uploads filed in a folder)
  UPLOADFOLDER  <folder path>

`uploads` has a row per uploaded file, served at /uploads/<name>. Its bytes live in the R2 bucket
`bucket` under `object_key` (see storage.py), or, for local dev and files from before R2, in `data`.
A file uploaded from the browser is `pending` until the API has seen it arrive in R2.
Sign-in sessions live in `sessions` (see auth.py).
"""

import os
from contextlib import contextmanager
from functools import cache

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get("EASYDOCS_DATABASE_URL", "postgresql://easydocs@localhost/easydocs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    pk   text NOT NULL,
    sk   text COLLATE "C" NOT NULL,
    data jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (pk, sk)
);
CREATE TABLE IF NOT EXISTS uploads (
    name         text PRIMARY KEY,
    content_type text NOT NULL,
    size         integer NOT NULL,
    data         bytea NOT NULL,
    modified     timestamptz NOT NULL DEFAULT now()
);
-- R2 metadata, added to the original table (where every file's bytes were in `data`).
ALTER TABLE uploads ALTER COLUMN data DROP NOT NULL;
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS bucket     text;
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS object_key text;
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS etag       text;
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS status     text NOT NULL DEFAULT 'ready';
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS created    timestamptz NOT NULL DEFAULT now();
CREATE UNIQUE INDEX IF NOT EXISTS uploads_object ON uploads (bucket, object_key);
DO $$ BEGIN
    ALTER TABLE uploads ADD CONSTRAINT uploads_status CHECK (status IN ('pending', 'ready'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
CREATE TABLE IF NOT EXISTS sessions (
    token_hash text PRIMARY KEY,
    expires    timestamptz NOT NULL
);
"""


class Exists(Exception):
    pass


class Missing(Exception):
    pass


@cache
def pool() -> ConnectionPool:
    return ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=True,
                          kwargs={"row_factory": dict_row})


@contextmanager
def db():
    """A connection, in a transaction that commits when the block ends (or rolls back on error)."""
    with pool().connection() as conn:
        yield conn


def init_schema() -> None:
    with db() as conn:
        conn.execute(SCHEMA)


# ---------- items ----------

def _row(r: dict) -> dict:
    return {"pk": r["pk"], "sk": r["sk"], **r["data"]}


def _like(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def query(pk: str, prefix: str = "", fields: list[str] | None = None, limit: int | None = None) -> list[dict]:
    """Every item under pk (whose sk starts with prefix), in sk order."""
    # With fields, only those keys come back, so listing docs doesn't drag every doc's content along.
    data = "data"
    params: list = []
    if fields:
        keys = [f for f in fields if f not in ("pk", "sk")]
        data = "jsonb_strip_nulls(jsonb_build_object(" + ", ".join("%s::text, data->%s" for _ in keys) + "))" if keys else "'{}'::jsonb"
        params += [x for k in keys for x in (k, k)]
    sql = f"SELECT pk, sk, {data} AS data FROM items WHERE pk = %s AND sk LIKE %s ORDER BY sk"
    params += [pk, _like(prefix)]
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    with db() as conn:
        return [_row(r) for r in conn.execute(sql, params)]


def any_with_prefix(pk: str, prefix: str) -> bool:
    return bool(query(pk, prefix, fields=["sk"], limit=1))


def get(pk: str, sk: str) -> dict | None:
    with db() as conn:
        r = conn.execute("SELECT pk, sk, data FROM items WHERE pk = %s AND sk = %s", (pk, sk)).fetchone()
    return _row(r) if r else None


def _split(item: dict) -> tuple[str, str, Jsonb]:
    return item["pk"], item["sk"], Jsonb({k: v for k, v in item.items() if k not in ("pk", "sk")})


def put(item: dict, new: bool = False) -> None:
    """Write an item. With new=True, fail with Exists instead of overwriting one."""
    with db() as conn:
        if new:
            try:
                conn.execute("INSERT INTO items (pk, sk, data) VALUES (%s, %s, %s)", _split(item))
            except errors.UniqueViolation as e:
                raise Exists from e
        else:
            conn.execute("INSERT INTO items (pk, sk, data) VALUES (%s, %s, %s) "
                         "ON CONFLICT (pk, sk) DO UPDATE SET data = EXCLUDED.data", _split(item))


def delete(pk: str, sk: str) -> None:
    """Delete an item, or raise Missing if there isn't one."""
    with db() as conn:
        if not conn.execute("DELETE FROM items WHERE pk = %s AND sk = %s", (pk, sk)).rowcount:
            raise Missing


def batch_write(puts: list[dict] = (), deletes: list[tuple[str, str]] = ()) -> None:
    """Puts and deletes in one transaction: all of it happens, or none of it."""
    with db() as conn, conn.cursor() as cur:
        if puts:
            cur.executemany("INSERT INTO items (pk, sk, data) VALUES (%s, %s, %s) "
                            "ON CONFLICT (pk, sk) DO UPDATE SET data = EXCLUDED.data", [_split(i) for i in puts])
        if deletes:
            cur.executemany("DELETE FROM items WHERE pk = %s AND sk = %s", list(deletes))


# ---------- uploaded files ----------

def upload_exists(name: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM uploads WHERE name = %s", (name,)).fetchone() is not None


def list_uploads() -> list[dict]:
    """Every uploaded file that has arrived: name, size and last-modified time."""
    with db() as conn:
        return list(conn.execute(
            "SELECT name, size, extract(epoch FROM modified)::bigint AS modified FROM uploads WHERE status = 'ready'"))


def get_upload(name: str) -> dict:
    """A file's row, without its bytes (has_data says whether they're in Postgres)."""
    with db() as conn:
        r = conn.execute("SELECT name, content_type, size, bucket, object_key, etag, status, "
                         "data IS NOT NULL AS has_data FROM uploads WHERE name = %s", (name,)).fetchone()
    if not r:
        raise Missing
    return r


def get_upload_data(name: str) -> bytes:
    with db() as conn:
        r = conn.execute("SELECT data FROM uploads WHERE name = %s AND data IS NOT NULL", (name,)).fetchone()
    if not r:
        raise Missing
    return bytes(r["data"])


def rename_upload(old: str, new: str) -> None:
    # Only the name (the URL) changes; the object in R2 keeps its key.
    with db() as conn:
        conn.execute("UPDATE uploads SET name = %s WHERE name = %s", (new, old))


def delete_upload(name: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM uploads WHERE name = %s", (name,))


def start_upload(name: str, content_type: str, size: int, bucket: str | None, object_key: str | None) -> None:
    """A row for a file that's on its way (status pending)."""
    with db() as conn:
        conn.execute("INSERT INTO uploads (name, content_type, size, bucket, object_key, status) "
                     "VALUES (%s, %s, %s, %s, %s, 'pending')", (name, content_type, size, bucket, object_key))


def finish_upload(name: str, size: int, etag: str | None) -> None:
    with db() as conn:
        conn.execute("UPDATE uploads SET status = 'ready', size = %s, etag = %s, modified = now() WHERE name = %s",
                     (size, etag, name))


def put_upload(name: str, data: bytes, content_type: str) -> None:
    """Keep a file's bytes in Postgres (local dev, without R2)."""
    with db() as conn:
        conn.execute("INSERT INTO uploads (name, content_type, size, data, status) VALUES (%s, %s, %s, %s, 'ready') "
                     "ON CONFLICT (name) DO UPDATE SET content_type = EXCLUDED.content_type, "
                     "size = EXCLUDED.size, data = EXCLUDED.data, status = 'ready', modified = now()",
                     (name, content_type, len(data), data))


def put_upload_object(name: str, content_type: str, size: int, bucket: str, object_key: str, etag: str) -> None:
    """Record a file already stored in R2."""
    with db() as conn:
        conn.execute("INSERT INTO uploads (name, content_type, size, bucket, object_key, etag, status) "
                     "VALUES (%s, %s, %s, %s, %s, %s, 'ready')", (name, content_type, size, bucket, object_key, etag))


def stale_uploads(hours: int = 24) -> list[dict]:
    """Uploads started but never finished (the browser closed, or the PUT failed)."""
    with db() as conn:
        return list(conn.execute("SELECT name, bucket, object_key FROM uploads WHERE status = 'pending' "
                                 "AND created < now() - make_interval(hours => %s)", (hours,)))


def uploads_in_postgres() -> list[str]:
    """Files whose bytes are still in Postgres, to move to R2 (scripts/uploads_to_r2.py)."""
    with db() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM uploads WHERE data IS NOT NULL AND object_key IS NULL")]


def moved_to_r2(name: str, bucket: str, object_key: str, etag: str) -> None:
    with db() as conn:
        conn.execute("UPDATE uploads SET bucket = %s, object_key = %s, etag = %s, data = NULL WHERE name = %s",
                     (bucket, object_key, etag, name))
