from app import main, store
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


def test_admin_needs_login(anon):
    assert anon.get("/admin/api/uploads").status_code == 401
    assert anon.post("/admin/api/docs", json={"path": "x"}).status_code == 401
    r = anon.get("/admin/edit/x", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login?next=/admin/edit/x"
    assert anon.get("/admin/login").status_code == 200
    # Reading stays public.
    assert anon.get("/api/tree").status_code == 200 and anon.get("/").status_code == 200
    assert anon.get("/api/session").json() == {"login_required": True, "signed_in": False}


def test_login_and_logout(anon):
    r = anon.post("/admin/login", data={"password": "wrong"}, follow_redirects=False)
    assert r.headers["location"].startswith("/admin/login?error=wrong")
    assert anon.get("/admin/api/uploads").status_code == 401

    # A next= that leaves the admin pages is ignored.
    r = anon.post("/admin/login", data={"password": PASSWORD, "next": "https://evil.example/"}, follow_redirects=False)
    assert r.headers["location"] == "/admin/"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert anon.get("/admin/api/uploads").status_code == 200
    assert anon.get("/api/session").json()["signed_in"] is True

    anon.post("/admin/logout")
    assert anon.get("/admin/api/uploads").status_code == 401


def test_logout_ends_the_session_server_side(client):
    token = client.cookies.get("mydocs_session")
    client.post("/admin/logout")
    client.cookies.set("mydocs_session", token)  # someone kept a copy of the cookie
    assert client.get("/admin/api/uploads").status_code == 401


def test_cross_site_writes_refused(client):
    r = client.post("/admin/api/folders", json={"path": "x"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.post("/admin/api/folders", json={"path": "x"}).status_code == 200


def test_login_throttled_after_many_failures(anon):
    for _ in range(10):
        anon.post("/admin/login", data={"password": "wrong"})
    r = anon.post("/admin/login", data={"password": PASSWORD}, follow_redirects=False)
    assert "error=throttled" in r.headers["location"]
