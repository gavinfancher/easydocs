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
Sign-in sessions live in `sessions`, and accounts in `users` (see auth.py).

Every item and upload has an `owner`, the user it belongs to, and each user only ever sees their
own. The functions here act for whoever is signed in (`OWNER`, set per request by the API) and
fail with LookupError if no one is, so nothing can read or write without an owner. The admin
account, whose password is EASYDOCS_ADMIN_PASSWORD_HASH, is named `admin`; it owns everything
from before there were users.
"""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ.get("EASYDOCS_DATABASE_URL", "postgresql://easydocs@localhost/easydocs")

ADMIN = "admin"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    name          text PRIMARY KEY,
    password_hash text NOT NULL,
    created       timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS items (
    owner text NOT NULL,
    pk    text NOT NULL,
    sk    text COLLATE "C" NOT NULL,
    data  jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (owner, pk, sk)
);
-- From before there were users: everything already here is the admin's.
ALTER TABLE items ADD COLUMN IF NOT EXISTS owner text NOT NULL DEFAULT 'admin';
ALTER TABLE items ALTER COLUMN owner DROP DEFAULT;
DO $$ BEGIN
    IF (SELECT indnatts FROM pg_index WHERE indexrelid = 'items_pkey'::regclass) <> 3 THEN
        ALTER TABLE items DROP CONSTRAINT items_pkey;
        ALTER TABLE items ADD PRIMARY KEY (owner, pk, sk);
    END IF;
END $$;
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
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS owner      text NOT NULL DEFAULT 'admin';
ALTER TABLE uploads ALTER COLUMN owner DROP DEFAULT;
CREATE UNIQUE INDEX IF NOT EXISTS uploads_object ON uploads (bucket, object_key);
DO $$ BEGIN
    ALTER TABLE uploads ADD CONSTRAINT uploads_status CHECK (status IN ('pending', 'ready'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
CREATE TABLE IF NOT EXISTS sessions (
    token_hash text PRIMARY KEY,
    expires    timestamptz NOT NULL
);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS username text NOT NULL DEFAULT 'admin';
ALTER TABLE sessions ALTER COLUMN username DROP DEFAULT;
"""


# Who the store is acting for. Unset means no one: every read and write below then fails.
OWNER: ContextVar[str] = ContextVar("owner")


@contextmanager
def as_owner(name: str):
    token = OWNER.set(name)
    try:
        yield
    finally:
        OWNER.reset(token)


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
    sql = f"SELECT pk, sk, {data} AS data FROM items WHERE owner = %s AND pk = %s AND sk LIKE %s ORDER BY sk"
    params += [OWNER.get(), pk, _like(prefix)]
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    with db() as conn:
        return [_row(r) for r in conn.execute(sql, params)]


def any_with_prefix(pk: str, prefix: str) -> bool:
    return bool(query(pk, prefix, fields=["sk"], limit=1))


def get(pk: str, sk: str) -> dict | None:
    with db() as conn:
        r = conn.execute("SELECT pk, sk, data FROM items WHERE owner = %s AND pk = %s AND sk = %s",
                         (OWNER.get(), pk, sk)).fetchone()
    return _row(r) if r else None


def _split(item: dict) -> tuple[str, str, str, Jsonb]:
    return OWNER.get(), item["pk"], item["sk"], Jsonb({k: v for k, v in item.items() if k not in ("pk", "sk")})


def put(item: dict, new: bool = False) -> None:
    """Write an item. With new=True, fail with Exists instead of overwriting one."""
    with db() as conn:
        if new:
            try:
                conn.execute("INSERT INTO items (owner, pk, sk, data) VALUES (%s, %s, %s, %s)", _split(item))
            except errors.UniqueViolation as e:
                raise Exists from e
        else:
            conn.execute("INSERT INTO items (owner, pk, sk, data) VALUES (%s, %s, %s, %s) "
                         "ON CONFLICT (owner, pk, sk) DO UPDATE SET data = EXCLUDED.data", _split(item))


def delete(pk: str, sk: str) -> None:
    """Delete an item, or raise Missing if there isn't one."""
    with db() as conn:
        if not conn.execute("DELETE FROM items WHERE owner = %s AND pk = %s AND sk = %s",
                            (OWNER.get(), pk, sk)).rowcount:
            raise Missing


def batch_write(puts: list[dict] = (), deletes: list[tuple[str, str]] = ()) -> None:
    """Puts and deletes in one transaction: all of it happens, or none of it."""
    owner = OWNER.get()
    with db() as conn, conn.cursor() as cur:
        if puts:
            cur.executemany("INSERT INTO items (owner, pk, sk, data) VALUES (%s, %s, %s, %s) "
                            "ON CONFLICT (owner, pk, sk) DO UPDATE SET data = EXCLUDED.data", [_split(i) for i in puts])
        if deletes:
            cur.executemany("DELETE FROM items WHERE owner = %s AND pk = %s AND sk = %s",
                            [(owner, pk, sk) for pk, sk in deletes])


# ---------- uploaded files ----------
# Names are unique across everyone (each has a random tag), but only their owner can see a file.
# The exceptions, marked, work on any owner's files: the cleanup of abandoned uploads, the move to
# R2, and serving a file by a signed link (which only its owner could have been given).

def upload_exists(name: str) -> bool:
    with db() as conn:
        return conn.execute("SELECT 1 FROM uploads WHERE name = %s AND owner = %s",
                            (name, OWNER.get())).fetchone() is not None


def list_uploads() -> list[dict]:
    """Every uploaded file that has arrived: name, size and last-modified time."""
    with db() as conn:
        return list(conn.execute(
            "SELECT name, size, extract(epoch FROM modified)::bigint AS modified FROM uploads "
            "WHERE status = 'ready' AND owner = %s", (OWNER.get(),)))


def get_upload(name: str, any_owner: bool = False) -> dict:
    """A file's row, without its bytes (has_data says whether they're in Postgres)."""
    sql = ("SELECT name, content_type, size, bucket, object_key, etag, status, "
           "data IS NOT NULL AS has_data FROM uploads WHERE name = %s")
    params = [name]
    if not any_owner:
        sql += " AND owner = %s"
        params.append(OWNER.get())
    with db() as conn:
        r = conn.execute(sql, params).fetchone()
    if not r:
        raise Missing
    return r


def get_upload_data(name: str) -> bytes:
    """Any owner's: only called after get_upload has checked who may have it."""
    with db() as conn:
        r = conn.execute("SELECT data FROM uploads WHERE name = %s AND data IS NOT NULL", (name,)).fetchone()
    if not r:
        raise Missing
    return bytes(r["data"])


def rename_upload(old: str, new: str) -> None:
    """Only the name (the URL) changes; the object in R2 keeps its key. Exists if the name's taken."""
    with db() as conn:
        try:
            conn.execute("UPDATE uploads SET name = %s WHERE name = %s AND owner = %s", (new, old, OWNER.get()))
        except errors.UniqueViolation as e:
            raise Exists from e


def delete_upload(name: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM uploads WHERE name = %s AND owner = %s", (name, OWNER.get()))


def start_upload(name: str, content_type: str, size: int, bucket: str | None, object_key: str | None) -> None:
    """A row for a file that's on its way (status pending)."""
    with db() as conn:
        conn.execute("INSERT INTO uploads (owner, name, content_type, size, bucket, object_key, status) "
                     "VALUES (%s, %s, %s, %s, %s, %s, 'pending')",
                     (OWNER.get(), name, content_type, size, bucket, object_key))


def finish_upload(name: str, size: int, etag: str | None) -> None:
    with db() as conn:
        conn.execute("UPDATE uploads SET status = 'ready', size = %s, etag = %s, modified = now() "
                     "WHERE name = %s AND owner = %s", (size, etag, name, OWNER.get()))


def put_upload(name: str, data: bytes, content_type: str) -> None:
    """Keep a file's bytes in Postgres (local dev, without R2)."""
    with db() as conn:
        conn.execute("INSERT INTO uploads (owner, name, content_type, size, data, status) "
                     "VALUES (%s, %s, %s, %s, %s, 'ready') "
                     "ON CONFLICT (name) DO UPDATE SET content_type = EXCLUDED.content_type, "
                     "size = EXCLUDED.size, data = EXCLUDED.data, status = 'ready', modified = now() "
                     "WHERE uploads.owner = EXCLUDED.owner",
                     (OWNER.get(), name, content_type, len(data), data))


def put_upload_object(name: str, content_type: str, size: int, bucket: str, object_key: str, etag: str) -> None:
    """Record a file already stored in R2."""
    with db() as conn:
        conn.execute("INSERT INTO uploads (owner, name, content_type, size, bucket, object_key, etag, status) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s, 'ready')",
                     (OWNER.get(), name, content_type, size, bucket, object_key, etag))


def stale_uploads(hours: int = 24) -> list[dict]:
    """Uploads started but never finished (the browser closed, or the PUT failed). Any owner's."""
    with db() as conn:
        return list(conn.execute("SELECT name, owner, bucket, object_key FROM uploads WHERE status = 'pending' "
                                 "AND created < now() - make_interval(hours => %s)", (hours,)))


def uploads_in_postgres() -> list[str]:
    """Files whose bytes are still in Postgres, to move to R2 (scripts/uploads_to_r2.py). Any owner's."""
    with db() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM uploads WHERE data IS NOT NULL AND object_key IS NULL")]


def moved_to_r2(name: str, bucket: str, object_key: str, etag: str) -> None:
    with db() as conn:
        conn.execute("UPDATE uploads SET bucket = %s, object_key = %s, etag = %s, data = NULL WHERE name = %s",
                     (bucket, object_key, etag, name))


# ---------- users ----------

def user_password_hash(name: str) -> str | None:
    with db() as conn:
        r = conn.execute("SELECT password_hash FROM users WHERE name = %s", (name,)).fetchone()
    return r["password_hash"] if r else None


def set_user(name: str, password_hash: str) -> str:
    """Create a user or change their password: 'new', 'changed' or 'same'.
    A changed password signs them out everywhere."""
    with db() as conn:
        r = conn.execute("INSERT INTO users (name, password_hash) VALUES (%s, %s) "
                         "ON CONFLICT (name) DO UPDATE SET password_hash = EXCLUDED.password_hash "
                         "WHERE users.password_hash <> EXCLUDED.password_hash "
                         "RETURNING (xmax = 0) AS new", (name, password_hash)).fetchone()
        if r and not r["new"]:
            conn.execute("DELETE FROM sessions WHERE username = %s", (name,))
    return "same" if not r else "new" if r["new"] else "changed"


def list_users() -> list[str]:
    with db() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM users ORDER BY name")]
