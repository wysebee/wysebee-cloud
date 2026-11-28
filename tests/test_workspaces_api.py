"""The workspace routes against a real database (skipped without WYSEBEE_TEST_DATABASE_URL)
and the file store. The caller is faked with the X-Test-User header, as in
test_skill_files_api.py."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import Header
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from apps.cloud.auth import User, current_user
from tests.conftest import make_client, needs_db

MAX_BYTES = 64
MAX_FILES = 4
MAX_COUNT = 2


def _fake_user(x_test_user: str = Header()) -> User:
    return User(sub=x_test_user)


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    return tmp_path / "user-workspaces"


@pytest.fixture
def client(ui_dist: Path, migrated_db_url: str, workspace_dir: Path) -> Iterator[TestClient]:
    with make_client(
        ui_dist,
        database_url=migrated_db_url,
        workspace_dir=workspace_dir,
        workspace_file_max_bytes=MAX_BYTES,
        workspace_max_files=MAX_FILES,
        workspace_max_count=MAX_COUNT,
    ) as c:
        c.app.dependency_overrides[current_user] = _fake_user
        yield c


@pytest.fixture
def users(migrated_db_url: str) -> Iterator[list[str]]:
    subs = [f"test-{uuid.uuid4().hex}" for _ in range(2)]
    yield subs
    engine = create_engine(migrated_db_url)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM user_workspaces WHERE user_id = ANY(:u)"), {"u": subs})
    engine.dispose()


def h(sub: str) -> dict[str, str]:
    return {"X-Test-User": sub}


def create(client, sub, name="My Docs"):
    return client.post("/api/v1/workspaces", json={"name": name}, headers=h(sub))


@needs_db
def test_a_workspace_is_a_row_and_a_folder_under_its_id(client, users, workspace_dir):
    a, b = users
    r = create(client, a, "  My Docs ")
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["name"] == "My Docs" and str(uuid.UUID(p["id"])) == p["id"]
    assert p["resource_url"].endswith(f"/user-workspaces/{a}/{p['id']}/")
    assert (workspace_dir / a / p["id"] / "README.md").read_text().startswith("# My Docs")
    assert [x["id"] for x in client.get("/api/v1/workspaces", headers=h(a)).json()["workspaces"]] == [p["id"]]
    assert client.get("/api/v1/workspaces", headers=h(b)).json() == {"workspaces": []}
    # Another person's workspace is not there for them.
    assert client.get(f"/api/v1/workspaces/{p['id']}", headers=h(b)).status_code == 404
    assert client.get(f"/api/v1/workspaces/{uuid.uuid4().hex}", headers=h(a)).status_code == 404
    assert client.get("/api/v1/workspaces/not-an-id", headers=h(a)).status_code == 422
    # One name per person, whatever its case; another person may use it.
    assert create(client, a, "my docs").status_code == 409
    assert create(client, b, "My Docs").status_code == 201
    for bad in ["", ".hidden", "a/b", "trailing.", "x" * 81, "tab\there"]:
        assert create(client, a, bad).status_code == 422, bad
    assert create(client, a, "Second").status_code == 201
    assert create(client, a, "Third").status_code == 409
    names = [x["name"] for x in client.get("/api/v1/workspaces", headers=h(a)).json()["workspaces"]]
    assert names == ["My Docs", "Second"]


@needs_db
def test_files_and_folders_make_the_tree(client, users, workspace_dir):
    a = users[0]
    pid = create(client, a).json()["id"]
    base = f"/api/v1/workspaces/{pid}"
    tree = client.get(base, headers=h(a)).json()
    assert [f["path"] for f in tree["files"]] == ["README.md"] and tree["folders"] == []
    r = client.put(f"{base}/files/guide/setup.md", json={"content": "# Setup\n"}, headers=h(a))
    assert r.status_code == 200 and r.json() == {"path": "guide/setup.md", "size": 8}
    assert client.post(f"{base}/folders/notes/drafts", headers=h(a)).status_code == 201
    # Again, or into a folder that already holds files: nothing more.
    assert client.post(f"{base}/folders/guide", headers=h(a)).status_code == 201
    tree = client.get(base, headers=h(a)).json()
    assert [f["path"] for f in tree["files"]] == ["README.md", "guide/setup.md"]
    assert tree["folders"] == ["guide", "notes", "notes/drafts"]
    got = client.get(f"{base}/files/guide/setup.md", headers=h(a)).json()
    assert got == {"path": "guide/setup.md", "content": "# Setup\n", "size": 8}
    assert client.get(f"{base}/files/nope.md", headers=h(a)).status_code == 404
    (workspace_dir / a / pid / "pic.bin").write_bytes(b"\xff\xfe")
    assert client.get(f"{base}/files/pic.bin", headers=h(a)).status_code == 422
    # A file where a folder is, a folder where a file is, and bad paths.
    assert client.put(f"{base}/files/guide", json={"content": "x"}, headers=h(a)).status_code == 409
    assert client.put(f"{base}/files/README.md/x.md", json={"content": "x"}, headers=h(a)).status_code == 409
    assert client.post(f"{base}/folders/README.md", headers=h(a)).status_code == 409
    for bad in ["a/%2E%2E/b.md", ".env", "/".join(["d"] * 11), "x" * 101]:
        assert client.put(f"{base}/files/{bad}", json={"content": "x"}, headers=h(a)).status_code in (404, 422), bad
    # Limits: the size of one file, the number of files.
    assert client.put(f"{base}/files/big.md", json={"content": "x" * (MAX_BYTES + 1)}, headers=h(a)).status_code == 413
    assert client.put(f"{base}/files/three.md", json={"content": ""}, headers=h(a)).status_code == 200
    assert client.put(f"{base}/files/four.md", json={"content": "x"}, headers=h(a)).status_code == 409
    assert client.put(f"{base}/files/three.md", json={"content": "again"}, headers=h(a)).status_code == 200
    # Deleting the last file of a folder keeps the folder.
    assert client.delete(f"{base}/files/guide/setup.md", headers=h(a)).status_code == 204
    assert client.delete(f"{base}/files/guide/setup.md", headers=h(a)).status_code == 404
    assert client.delete(f"{base}/files/three.md", headers=h(a)).status_code == 204
    assert "guide" in client.get(base, headers=h(a)).json()["folders"]
    assert client.delete(f"{base}/folders/notes", headers=h(a)).status_code == 204
    assert client.delete(f"{base}/folders/notes", headers=h(a)).status_code == 404
    assert "notes" not in client.get(base, headers=h(a)).json()["folders"]


@needs_db
def test_deleting_a_workspace_deletes_its_folder(client, users, workspace_dir, monkeypatch):
    a = users[0]
    pid = create(client, a).json()["id"]
    assert (workspace_dir / a / pid).is_dir()
    assert client.delete(f"/api/v1/workspaces/{pid}", headers=h(a)).status_code == 204
    assert not (workspace_dir / a / pid).exists()
    assert client.delete(f"/api/v1/workspaces/{pid}", headers=h(a)).status_code == 404
    # A store that fails afterwards does not undo the delete.
    pid = create(client, a).json()["id"]

    async def boom(*_):
        raise RuntimeError("store down")

    monkeypatch.setattr(client.app.state.workspace_store, "delete_prefix", boom)
    assert client.delete(f"/api/v1/workspaces/{pid}", headers=h(a)).status_code == 204
    assert client.get("/api/v1/workspaces", headers=h(a)).json() == {"workspaces": []}


@needs_db
def test_an_account_id_that_cannot_name_a_folder_holds_no_workspace(client):
    assert create(client, "../evil").status_code == 422


@needs_db
def test_a_workspace_is_renamed_and_its_files_stay(client, users, workspace_dir):
    a, b = users
    pid = create(client, a).json()["id"]
    create(client, a, "Second")
    url = f"/api/v1/workspaces/{pid}"
    renamed = client.put(url, json={"name": " Guides "}, headers=h(a))
    assert renamed.status_code == 200 and (renamed.json()["id"], renamed.json()["name"]) == (pid, "Guides")
    assert [f["path"] for f in client.get(url, headers=h(a)).json()["files"]] == ["README.md"]
    assert (workspace_dir / a / pid / "README.md").is_file()
    # Its own name in another case is fine; another workspace's, in any case, is not.
    assert client.put(url, json={"name": "GUIDES"}, headers=h(a)).status_code == 200
    assert client.put(url, json={"name": "second"}, headers=h(a)).status_code == 409
    assert client.put(url, json={"name": ".hidden"}, headers=h(a)).status_code == 422
    assert client.put(url, json={"name": "Theirs"}, headers=h(b)).status_code == 404
    # The id is taken with or without its dashes; anything else is not one.
    assert client.get(f"/api/v1/workspaces/{pid.replace('-', '')}", headers=h(a)).json()["name"] == "GUIDES"
    assert client.get(f"/api/v1/workspaces/{'-' * 36}", headers=h(a)).status_code == 404


@needs_db
def test_a_folder_is_renamed_where_it_is_with_all_it_holds(client, users, workspace_dir):
    a, b = users
    base = f"/api/v1/workspaces/{create(client, a).json()['id']}"
    for path in ("guide/setup.md", "guide/deep/more.md", "other/x.md"):
        assert client.put(f"{base}/files/{path}", json={"content": path}, headers=h(a)).status_code == 200
    assert client.post(f"{base}/folders/guide/empty", headers=h(a)).status_code == 201
    rename = lambda path, name, who=a: client.patch(f"{base}/folders/{path}", json={"name": name}, headers=h(who))  # noqa: E731

    r = rename("guide", "  Handbook ")
    assert (r.status_code, r.json()) == (200, {"path": "Handbook"})
    tree = client.get(base, headers=h(a)).json()
    assert [f["path"] for f in tree["files"]] == ["Handbook/deep/more.md", "Handbook/setup.md", "README.md", "other/x.md"]
    assert tree["folders"] == ["Handbook", "Handbook/deep", "Handbook/empty", "other"]
    assert client.get(f"{base}/files/Handbook/deep/more.md", headers=h(a)).json()["content"] == "guide/deep/more.md"
    # A folder inside one, renamed where it is.
    assert rename("Handbook/deep", "Deeper").json() == {"path": "Handbook/Deeper"}
    # Its case alone.
    assert rename("other", "Other").json() == {"path": "Other"}
    assert client.get(f"{base}/files/Other/x.md", headers=h(a)).json()["content"] == "other/x.md"
    assert "other" not in client.get(base, headers=h(a)).json()["folders"]
    # Its own name: nothing moves.
    assert rename("Other", "Other").json() == {"path": "Other"}
    # Taken there, by a folder or a file: refused, nothing moved.
    assert rename("Other", "Handbook").status_code == 409
    assert rename("Other", "handbook").status_code == 409
    assert rename("Other", "README.md").status_code == 409
    assert rename("Handbook/Deeper", "setup.md").status_code == 409
    assert "Other/x.md" in [f["path"] for f in client.get(base, headers=h(a)).json()["files"]]
    # Not a name; not there; another person's.
    assert rename("Other", "a/b").status_code == 422
    assert rename("Other", ".hidden").status_code == 422
    assert rename("Other", " ").status_code == 422
    assert rename("nope", "x").status_code == 404
    assert rename("README.md", "x").status_code == 404
    assert rename("Other", "x", who=b).status_code == 404


@needs_db
def test_a_file_is_renamed_where_it_is(client, users):
    a, b = users
    base = f"/api/v1/workspaces/{create(client, a).json()['id']}"
    for path in ("guide/setup.md", "guide/page.html", "other.md"):
        assert client.put(f"{base}/files/{path}", json={"content": path}, headers=h(a)).status_code == 200
    rename = lambda path, name, who=a: client.patch(f"{base}/files/{path}", json={"name": name}, headers=h(who))  # noqa: E731

    assert rename("guide/setup.md", " Install.md ").json() == {"path": "guide/Install.md"}
    assert client.get(f"{base}/files/guide/Install.md", headers=h(a)).json()["content"] == "guide/setup.md"
    assert client.get(f"{base}/files/guide/setup.md", headers=h(a)).status_code == 404
    # Its case alone; its own name.
    assert rename("other.md", "Other.md").json() == {"path": "Other.md"}
    assert [f["path"] for f in client.get(base, headers=h(a)).json()["files"]] == ["Other.md", "README.md", "guide/Install.md", "guide/page.html"]
    assert rename("Other.md", "Other.md").json() == {"path": "Other.md"}
    # Taken there, by a file (whatever its case) or a folder: refused.
    assert rename("Other.md", "readme.md").status_code == 409
    assert rename("Other.md", "guide").status_code == 409
    # A page stays a page, a document a document.
    assert rename("guide/page.html", "page.md").status_code == 422
    assert rename("Other.md", "other.html").status_code == 422
    # Not a name; not there; a folder; another person's.
    assert rename("Other.md", "a/b.md").status_code == 422
    assert rename("Other.md", ".hidden").status_code == 422
    assert rename("nope.md", "x.md").status_code == 404
    assert rename("guide", "x").status_code == 404
    assert rename("Other.md", "x.md", who=b).status_code == 404

