from datetime import datetime, timezone

import pytest

from app import main, storage, store
from tests.conftest import PASSWORD


def create(client, path, content=""):
    r = client.post("/admin/api/docs", json={"path": path, "content": content})
    assert r.status_code == 200, r.text
    return r.json()


def test_docs_crud_and_tree(client):
    create(client, "guides/setup", "# Setting up\n\nInstall it.")
    create(client, "index", "hello")
    assert client.post("/admin/api/docs", json={"path": "index"}).status_code == 409

    assert client.get("/api/docs/guides/setup").json() == {
        "path": "guides/setup", "title": "Setting up", "content": "# Setting up\n\nInstall it."}
    assert client.get("/api/docs/index").json()["title"] == "Index"  # no heading: named after the file

    r = client.put("/admin/api/docs/index", json={"content": "# Home\n"})
    assert r.json() == {"path": "index", "title": "Home"}

    assert client.get("/api/tree").json() == [
        {"type": "folder", "name": "guides", "path": "guides", "children": [
            {"type": "doc", "name": "setup", "path": "guides/setup", "title": "Setting up"}]},
        {"type": "doc", "name": "index", "path": "index", "title": "Home"},
    ]

    assert client.delete("/admin/api/docs/index").status_code == 200
    assert client.get("/api/docs/index").status_code == 404
    assert client.delete("/admin/api/docs/index").status_code == 404


def test_bad_paths_rejected(client):
    for path in ("../etc/passwd", ".hidden/x", "/"):
        assert client.post("/admin/api/docs", json={"path": path}).status_code == 400


def test_doc_size_limit(client):
    r = client.post("/admin/api/docs", json={"path": "big", "content": "x" * (main.MAX_DOC_BYTES + 1)})
    assert r.status_code == 413


def test_folders(client):
    client.post("/admin/api/folders", json={"path": "a/b"})
    tree = client.get("/api/tree").json()
    assert tree == [{"type": "folder", "name": "a", "path": "a", "children": [
        {"type": "folder", "name": "b", "path": "a/b", "children": []}]}]
    assert client.delete("/admin/api/folders/a").status_code == 409  # has a/b in it
    assert client.delete("/admin/api/folders/a/b").status_code == 200
    assert client.delete("/admin/api/folders/a").status_code == 200
    assert client.get("/api/tree").json() == []


def test_empty_folder_survives_moving_its_last_doc_out(client):
    create(client, "a/doc")
    client.post("/admin/api/move", json={"src": "a/doc", "dst": "doc"})
    assert [n["path"] for n in client.get("/api/tree").json()] == ["a", "doc"]


def test_move_doc(client):
    create(client, "old", "# Title")
    create(client, "taken")
    assert client.post("/admin/api/move", json={"src": "old", "dst": "taken"}).status_code == 409
    assert client.post("/admin/api/move", json={"src": "old", "dst": "x/new"}).json() == {"path": "x/new"}
    assert client.get("/api/docs/old").status_code == 404
    assert client.get("/api/docs/x/new").json()["title"] == "Title"


def test_move_folder(client):
    create(client, "a/one")
    create(client, "a/sub/two")
    create(client, "ab/three")  # shares a prefix with "a" but isn't inside it
    assert client.post("/admin/api/move", json={"src": "a", "dst": "a/sub/x"}).status_code == 400
    assert client.post("/admin/api/move", json={"src": "a", "dst": "ab"}).status_code == 409
    assert client.post("/admin/api/move", json={"src": "a", "dst": "z/a"}).status_code == 200
    paths = sorted(d["sk"] for d in store.query("DOC"))
    assert paths == ["ab/three", "z/a/one", "z/a/sub/two"]
    folders = sorted(f["sk"] for f in store.query("FOLDER"))
    assert folders == ["ab", "z", "z/a", "z/a/sub"]


def test_home_and_search(client):
    create(client, "notes", "# Notes\n\nThe quick brown fox.")
    home = client.get("/api/home").json()
    assert home["show_recent"] is True and home["recent"][0]["path"] == "notes"
    saved = client.put("/admin/api/home", json={"heading": "Hi", "show_recent": False}).json()
    assert saved["heading"] == "Hi" and saved["show_recent"] is False

    assert client.get("/api/search", params={"q": "BROWN"}).json()[0]["path"] == "notes"
    assert client.get("/api/search", params={"q": "nothing"}).json() == []


def post_file(client, filename, data=b"png", content_type="image/png", folder=""):
    return client.post("/admin/api/uploads", files={"file": (filename, data, content_type)}, data={"folder": folder})


def upload(client, filename="Screen Shot.png", folder="", data=b"png"):
    r = post_file(client, filename, data, folder=folder)
    assert r.status_code == 200, r.text
    return r.json()


def test_upload_validation(client):
    assert post_file(client, "x.exe", content_type="application/octet-stream").status_code == 400
    assert post_file(client, "x.png", b"x" * (main.MAX_UPLOAD_BYTES + 1)).status_code == 413
    # Pasted images may have no extension; the content type fills it in.
    assert post_file(client, "image").json()["url"].endswith(".png")


def test_uploads_are_served(client, anon):
    url = upload(client, data=b"\x89PNG")["url"]
    r = anon.get(url)
    assert r.status_code == 200 and r.content == b"\x89PNG" and r.headers["content-type"] == "image/png"
    assert "sandbox" in r.headers["content-security-policy"]
    assert anon.get("/uploads/nope.png").status_code == 404


def test_uploads_list_rename_delete(client):
    res = upload(client, folder="shots")
    name = res["url"].removeprefix("/uploads/")
    assert res["name"] == "Screen Shot" and res["is_image"] and name.startswith("screen-shot-")
    create(client, "doc", f"![screen-shot]({res['url']})")
    client.put("/admin/api/home", json={"body": f"see {res['url']}"})

    listing = client.get("/admin/api/uploads").json()
    assert listing["folders"] == ["shots"]
    [f] = listing["files"]
    assert (f["name"], f["folder"], f["used_in"], f["on_home"], f["size"]) == (name, "shots", ["doc"], True, 3)

    r = client.post(f"/admin/api/uploads/{name}/rename", json={"name": "diagram"}).json()
    new = r["name"]
    assert new.startswith("diagram-") and r["docs"] == ["doc"]
    assert client.get("/api/docs/doc").json()["content"] == f"![diagram](/uploads/{new})"
    assert client.get("/api/home").json()["body"] == f"see /uploads/{new}"
    assert client.get("/admin/api/uploads").json()["files"][0]["folder"] == "shots"  # kept its folder

    assert client.delete(f"/admin/api/uploads/{new}").status_code == 200
    assert client.get("/admin/api/uploads").json()["files"] == []
    assert client.delete(f"/admin/api/uploads/{new}").status_code == 404


def test_upload_folders(client):
    name = upload(client)["url"].removeprefix("/uploads/")
    client.post("/admin/api/upload-folders", json={"path": "a/b"})
    client.post("/admin/api/uploads/move", json={"names": [name], "folder": "a/b"})
    assert client.post("/admin/api/upload-folders/move", json={"src": "a", "dst": "c"}).status_code == 200
    listing = client.get("/admin/api/uploads").json()
    assert listing["folders"] == ["c", "c/b"] and listing["files"][0]["folder"] == "c/b"
    client.post("/admin/api/upload-folders/delete", json={"path": "c"})
    listing = client.get("/admin/api/uploads").json()
    assert listing["folders"] == ["b"] and listing["files"][0]["folder"] == "b"
    assert client.post("/admin/api/uploads/move", json={"names": ["nope.png"]}).status_code == 404


def test_api_needs_a_session(anon):
    for path in ("/api/tree", "/api/docs/index", "/api/config", "/admin/api/uploads"):
        assert anon.get(path).status_code == 401, path
    assert anon.post("/admin/api/docs", json={"path": "x"}).status_code == 401
    assert anon.get("/uploads/x.png").status_code == 401
    assert anon.get("/api/session").json() == {"login_required": True, "signed_in": False}
    # The pages are open: they hold no docs, and send you to sign in when the API says 401.
    for path in ("/", "/docs/guides/setup", "/admin/login", "/admin/edit/x", "/static/app.css"):
        assert anon.get(path).status_code == 200, path


def test_login_and_logout(anon):
    r = anon.post("/api/login", json={"password": "wrong"})
    assert r.status_code == 401 and "isn’t right" in r.json()["detail"]
    token = anon.post("/api/login", json={"password": PASSWORD}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    assert anon.get("/admin/api/uploads", headers=auth).status_code == 200
    assert anon.get("/api/session", headers=auth).json()["signed_in"] is True
    assert anon.get("/admin/api/uploads", headers={"Authorization": "Bearer nope"}).status_code == 401

    # Signing out ends the session on the server, so a kept copy of the token stops working.
    anon.post("/api/logout", headers=auth)
    assert anon.get("/admin/api/uploads", headers=auth).status_code == 401


def test_login_throttled_after_many_failures(anon):
    for _ in range(10):
        anon.post("/api/login", json={"password": "wrong"})
    assert anon.post("/api/login", json={"password": PASSWORD}).status_code == 429


def test_cors_for_the_site(monkeypatch):
    """The pages (easydocs.gavinf.com) call the API on another hostname, so it answers CORS preflights."""
    from fastapi.testclient import TestClient
    from fastapi.middleware.cors import CORSMiddleware
    site = "https://easydocs.example"
    app = CORSMiddleware(main.app, allow_origins=[site], **main.CORS_SETTINGS)  # as main.py adds it
    c = TestClient(app)
    r = c.options("/api/tree", headers={"Origin": site, "Access-Control-Request-Method": "GET",
                                         "Access-Control-Request-Headers": "authorization"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == site
    r = c.get("/api/tree", headers={"Origin": site})
    assert r.status_code == 401 and r.headers["access-control-allow-origin"] == site  # so the page can see the 401
    r = c.options("/api/tree", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in r.headers


# ---------- uploads from the browser, and R2 ----------

def start(client, filename="Shot.png", size=4, **extra):
    r = client.post("/admin/api/uploads/start", json={"filename": filename, "content_type": "image/png", "size": size, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def test_browser_upload_without_r2(client):
    s = start(client)
    assert s["name"] == "Shot" and s["upload"]["url"] == f"/admin/api/uploads/{s['file']}/data"
    # Not listed or served until it's finished.
    assert client.get("/admin/api/uploads").json()["files"] == []
    assert client.post(f"/admin/api/uploads/{s['file']}/finish", json={}).status_code == 400
    assert client.put(s["upload"]["url"], content=b"\x89PNG").status_code == 200
    r = client.post(f"/admin/api/uploads/{s['file']}/finish", json={"folder": "shots"}).json()
    assert r == {"url": f"/uploads/{s['file']}", "is_image": True, "folder": "shots"}
    assert client.get(r["url"]).content == b"\x89PNG"
    [f] = client.get("/admin/api/uploads").json()["files"]
    assert (f["name"], f["folder"], f["size"]) == (s["file"], "shots", 4)


def test_browser_upload_checks_type_and_size(client):
    assert client.post("/admin/api/uploads/start", json={"filename": "x.exe", "size": 1}).status_code == 400
    r = client.post("/admin/api/uploads/start", json={"filename": "x.png", "size": main.MAX_UPLOAD_BYTES + 1})
    assert r.status_code == 413


@pytest.fixture
def r2(monkeypatch):
    """R2 turned on, with a dict of key -> bytes standing in for the bucket."""
    objects = {}
    monkeypatch.setattr(storage, "BUCKET", "docs")
    monkeypatch.setattr(storage, "enabled", lambda: True)
    monkeypatch.setattr(storage, "upload_url",
                        lambda key, ct: {"method": "PUT", "url": f"https://r2/put/{key}", "headers": {"Content-Type": ct}})
    monkeypatch.setattr(storage, "download_url", lambda key, bucket=None: f"https://r2/get/{bucket}/{key}")
    monkeypatch.setattr(storage, "head", lambda key, bucket=None:
                        {"size": len(objects[key]), "content_type": "image/png", "etag": "e1"} if key in objects else None)
    monkeypatch.setattr(storage, "put", lambda key, data, ct: objects.__setitem__(key, data) or "e2")
    monkeypatch.setattr(storage, "delete", lambda key, bucket=None: objects.pop(key, None))
    return objects


def test_browser_upload_to_r2(client, r2):
    s = start(client)
    key = s["upload"]["url"].removeprefix("https://r2/put/")
    assert key.startswith("uploads/") and key.endswith(".png")
    assert client.post(f"/admin/api/uploads/{s['file']}/finish", json={}).status_code == 400  # not there yet
    r2[key] = b"\x89PNG"  # the browser's PUT
    url = client.post(f"/admin/api/uploads/{s['file']}/finish", json={}).json()["url"]
    r = client.get(url, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"https://r2/get/docs/{key}"
    assert "private" in r.headers["cache-control"]

    # Renaming changes the URL but not the object; deleting removes the object too.
    new = client.post(f"/admin/api/uploads/{s['file']}/rename", json={"name": "diagram"}).json()["name"]
    assert client.get(f"/uploads/{new}", follow_redirects=False).headers["location"].endswith(key)
    client.delete(f"/admin/api/uploads/{new}")
    assert r2 == {}


def test_r2_upload_too_big_is_removed(client, r2):
    s = start(client)
    key = s["upload"]["url"].removeprefix("https://r2/put/")
    r2[key] = b"x" * (main.MAX_UPLOAD_BYTES + 1)  # claimed 4 bytes, sent more
    assert client.post(f"/admin/api/uploads/{s['file']}/finish", json={}).status_code == 413
    assert r2 == {} and client.get(f"/uploads/{s['file']}").status_code == 404


def test_form_upload_goes_to_r2(client, r2):
    url = upload(client, data=b"\x89PNG")["url"]
    assert list(r2.values()) == [b"\x89PNG"]
    assert client.get(url, follow_redirects=False).status_code == 302


def test_presign_matches_aws_example():
    # The presigned-URL example in AWS's S3 docs ("Authenticating Requests: Using Query Parameters").
    url = storage.presign_url("GET", "examplebucket.s3.amazonaws.com", "/test.txt",
                              access_key="AKIAIOSFODNN7EXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                              region="us-east-1", now=datetime(2013, 5, 24, tzinfo=timezone.utc), expires=86400)
    assert url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")


# ---------- links for uploads in pages ----------

def test_upload_links_without_r2(client, anon):
    url = upload(client, data=b"\x89PNG")["url"]
    name = url.removeprefix("/uploads/")
    links = client.post("/api/uploads/links", json={"names": [name, "nope.png"]}).json()["links"]
    assert list(links) == [name] and links[name].startswith(url + "?expires=")
    # An <img> can't send the token; the signed link works without it.
    del client.headers["Authorization"]
    assert anon.get(links[name]).content == b"\x89PNG"
    assert anon.get(links[name].replace("sig=", "sig=0")).status_code == 401
    assert anon.get(url).status_code == 401


def test_upload_links_with_r2(client, r2):
    s = start(client)
    key = s["upload"]["url"].removeprefix("https://r2/put/")
    r2[key] = b"\x89PNG"
    client.post(f"/admin/api/uploads/{s['file']}/finish", json={})
    assert client.post("/api/uploads/links", json={"names": [s["file"]]}).json()["links"] == {s["file"]: f"https://r2/get/docs/{key}"}


def test_probes_need_no_session(anon):
    assert anon.get("/healthz").json() == {"status": "ok"}
    assert anon.get("/readyz").json() == {"status": "ready"}
