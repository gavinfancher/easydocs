"""Sign-in for the admin pages: one admin password, and a session cookie once you've given it.

The password is only kept as a scrypt hash, in EASYDOCS_ADMIN_PASSWORD_HASH. Make one with

    uv run python -m app.auth

Sessions are random tokens. The cookie holds the token; the database holds only its hash, so a
leaked database can't be used to sign in, and signing out really ends the session.
"""

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

from app import store

PASSWORD_HASH = os.environ.get("EASYDOCS_ADMIN_PASSWORD_HASH", "")
COOKIE = "easydocs_session"
SESSION_DAYS = 30
SCRYPT = {"n": 2**15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024}

# A few wrong passwords are fine; a stream of them gets a pause. Per client IP, in memory.
MAX_FAILURES, FAILURE_WINDOW = 10, 15 * 60
_failures: dict[str, deque] = defaultdict(deque)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **SCRYPT)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt:{b64(salt)}:{b64(digest)}"


def check_password(password: str, stored: str = "") -> bool:
    stored = stored or PASSWORD_HASH
    try:
        scheme, salt, digest = stored.split(":")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), **SCRYPT)
        return hmac.compare_digest(actual, base64.b64decode(digest))
    except Exception:
        return False


def throttled(ip: str) -> bool:
    tries = _failures[ip]
    while tries and tries[0] < time.time() - FAILURE_WINDOW:
        tries.popleft()
    return len(tries) >= MAX_FAILURES


def record_failure(ip: str) -> None:
    _failures[ip].append(time.time())


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session() -> str:
    token = secrets.token_urlsafe(32)
    with store.db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < now()")
        conn.execute("INSERT INTO sessions (token_hash, expires) VALUES (%s, now() + make_interval(days => %s))",
                     (_hash(token), SESSION_DAYS))
    return token


def valid_session(token: str | None) -> bool:
    if not token:
        return False
    with store.db() as conn:
        return conn.execute("SELECT 1 FROM sessions WHERE token_hash = %s AND expires > now()",
                            (_hash(token),)).fetchone() is not None


def end_session(token: str | None) -> None:
    if token:
        with store.db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash(token),))


if __name__ == "__main__":
    import getpass

    pw = getpass.getpass("New admin password: ")
    if len(pw) < 12:
        raise SystemExit("Use at least 12 characters.")
    if getpass.getpass("Again: ") != pw:
        raise SystemExit("Those didn't match.")
    print(hash_password(pw))
