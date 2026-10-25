import os

import pytest

# Tests need a Postgres to talk to; `make test` starts one in Docker. They wipe its tables.
os.environ["EASYDOCS_DATABASE_URL"] = os.environ.get(
    "EASYDOCS_TEST_DATABASE_URL", "postgresql://postgres:test@localhost:5433/postgres")
os.environ.pop("EASYDOCS_LOCAL", None)

from app import auth  # noqa: E402

PASSWORD = "correct horse battery staple"
auth.PASSWORD_HASH = os.environ["EASYDOCS_ADMIN_PASSWORD_HASH"] = auth.hash_password(PASSWORD)

from fastapi.testclient import TestClient  # noqa: E402

from app import main, store  # noqa: E402

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def anon():
    """A visitor who hasn't signed in."""
    store.init_schema()
    with store.db() as conn:
        conn.execute("TRUNCATE items, uploads, sessions")
    auth._failures.clear()
    yield TestClient(main.app, headers=ORIGIN)


@pytest.fixture
def client(anon):
    """A signed-in admin."""
    r = anon.post("/admin/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return anon
