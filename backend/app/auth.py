"""Sign-in: a username and password, and a session token once you've given them.

Accounts are in the `users` table, each password only as a scrypt hash. The admin account
(`admin`) takes its password from EASYDOCS_ADMIN_PASSWORD_HASH, set again on every start. Make
that hash with

    uv run python -m app.auth

Add another user, or change one's password, against the database the API uses (in production,
inside the API's pod; see README):

    uv run python -m app.auth set-user <name>

Sessions are random tokens. The pages keep the token and send it as `Authorization: Bearer <token>`;
the database holds only its hash and whose session it is, so a leaked database can't be used to
sign in, and signing out really ends the session.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from collections import defaultdict, deque

from app import store

PASSWORD_HASH = os.environ.get("EASYDOCS_ADMIN_PASSWORD_HASH", "")
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


USERNAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,39}")
# Checked against when there's no such user, so a wrong name takes as long as a wrong password.
_NO_USER = hash_password(secrets.token_urlsafe(16))


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split(":")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), **SCRYPT)
        return hmac.compare_digest(actual, base64.b64decode(digest))
    except Exception:
        return False


def verify(username: str, password: str) -> bool:
    stored = store.user_password_hash(username) if USERNAME.fullmatch(username) else None
    return check_password(password, stored or _NO_USER) and stored is not None


def seed_admin() -> None:
    if PASSWORD_HASH:
        store.set_user(store.ADMIN, PASSWORD_HASH)


def throttled(ip: str) -> bool:
    tries = _failures[ip]
    while tries and tries[0] < time.time() - FAILURE_WINDOW:
        tries.popleft()
    return len(tries) >= MAX_FAILURES


def record_failure(ip: str) -> None:
    _failures[ip].append(time.time())


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    with store.db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < now()")
        conn.execute("INSERT INTO sessions (token_hash, username, expires) "
                     "VALUES (%s, %s, now() + make_interval(days => %s))", (_hash(token), username, SESSION_DAYS))
    return token


def session_user(token: str | None) -> str | None:
    """Who a session token belongs to, or None if it's not a live session."""
    if not token:
        return None
    with store.db() as conn:
        r = conn.execute("SELECT username FROM sessions WHERE token_hash = %s AND expires > now()",
                         (_hash(token),)).fetchone()
    return r["username"] if r else None


def end_session(token: str | None) -> None:
    if token:
        with store.db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash(token),))


def _ask_password(prompt: str) -> str:
    import getpass

    pw = getpass.getpass(prompt)
    if len(pw) < 10:
        raise SystemExit("Use at least 10 characters.")
    if getpass.getpass("Again: ") != pw:
        raise SystemExit("Those didn't match.")
    return pw


def _set_user(name: str) -> None:
    name = name.strip().lower()
    if not USERNAME.fullmatch(name):
        raise SystemExit("Usernames are up to 40 lowercase letters, digits, '.', '_' or '-'.")
    if name == store.ADMIN:
        raise SystemExit("The admin password comes from EASYDOCS_ADMIN_PASSWORD_HASH. Change it there.")
    store.init_schema()
    result = store.set_user(name, hash_password(_ask_password(f"Password for {name}: ")))
    if result == "new":
        from app.main import seed_welcome  # only now: importing the app checks its settings
        seed_welcome(name)
    print({"new": f"Added {name}.", "changed": f"Changed {name}'s password; they're signed out everywhere.",
           "same": f"{name}'s password is unchanged."}[result])


if __name__ == "__main__":
    import sys

    match sys.argv[1:]:
        case []:
            print(hash_password(_ask_password("New admin password: ")))
        case ["set-user", name]:
            _set_user(name)
        case ["list-users"]:
            print("\n".join(store.list_users()))
        case _:
            raise SystemExit("Usage: python -m app.auth [set-user <name> | list-users]")
