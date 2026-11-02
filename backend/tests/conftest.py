import os

import pytest

# Tests need a Postgres to talk to; `make test` starts one in Docker. They wipe its tables.
os.environ["EASYDOCS_DATABASE_URL"] = os.environ.get(
    "EASYDOCS_TEST_DATABASE_URL", "postgresql://postgres:test@localhost:5433/postgres")
os.environ.pop("EASYDOCS_LOCAL", None)

from app import auth  # noqa: E402

PASSWORD = "correct horse battery staple"
OTHER_PASSWORD = "another long password here"
auth.PASSWORD_HASH = os.environ["EASYDOCS_ADMIN_PASSWORD_HASH"] = auth.hash_password(PASSWORD)

from fastapi.testclient import TestClient  # noqa: E402

from app import main, store  # noqa: E402

@pytest.fixture
def anon():
    """A visitor who hasn't signed in."""
    store.init_schema()
    with store.db() as conn:
        conn.execute("TRUNCATE items, uploads, sessions, users")
    auth.seed_admin()
    store.set_user("gfancher", auth.hash_password(OTHER_PASSWORD))
    auth._failures.clear()
    yield TestClient(main.app)


def signed_in(username: str, password: str) -> TestClient:
    c = TestClient(main.app)
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    c.headers["Authorization"] = "Bearer " + r.json()["token"]
    return c


@pytest.fixture
def client(anon):
    """A signed-in admin: every request carries the session token."""
    return signed_in("admin", PASSWORD)


@pytest.fixture
def other(anon):
    """A second user, gfancher, signed in."""
    return signed_in("gfancher", OTHER_PASSWORD)
