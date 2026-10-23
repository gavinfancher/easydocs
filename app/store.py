"""Storage: Postgres holds everything, docs and settings in `items`, uploaded files in `uploads`.

`items` is keyed by pk (what kind of thing) + sk (its path or name), with the rest in a jsonb column:

  pk            sk              data
  DOC           <doc path>      title, content, updated
  FOLDER        <folder path>   (every doc folder, so empty folders stick around)
  SITE          HOME            home page settings
  UPLOAD        <file name>     folder (only for uploads filed in a folder)
  UPLOADFOLDER  <folder path>

`uploads` holds each file's bytes; the app serves them at /uploads/<name>.
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
    """Every uploaded file: name, size and last-modified time."""
    with db() as conn:
        return list(conn.execute(
            "SELECT name, size, extract(epoch FROM modified)::bigint AS modified FROM uploads"))


def rename_upload(old: str, new: str) -> None:
    with db() as conn:
        conn.execute("UPDATE uploads SET name = %s WHERE name = %s", (new, old))


def delete_upload(name: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM uploads WHERE name = %s", (name,))


def get_upload(name: str) -> tuple[bytes, str]:
    with db() as conn:
        r = conn.execute("SELECT data, content_type FROM uploads WHERE name = %s", (name,)).fetchone()
    if not r:
        raise Missing
    return bytes(r["data"]), r["content_type"]


def put_upload(name: str, data: bytes, content_type: str) -> None:
    with db() as conn:
        conn.execute("INSERT INTO uploads (name, content_type, size, data) VALUES (%s, %s, %s, %s) "
                     "ON CONFLICT (name) DO UPDATE SET content_type = EXCLUDED.content_type, "
                     "size = EXCLUDED.size, data = EXCLUDED.data, modified = now()",
                     (name, content_type, len(data), data))
