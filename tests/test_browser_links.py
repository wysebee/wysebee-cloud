"""The browser link (apps/cloud/browser_links.py): the Wysebee Agent extension pairs with
the cloud and is driven through it — every route and refusal of
docs/protocol/browser-extension.md, against a real database (skipped
without WYSEBEE_TEST_DATABASE_URL). The person is faked with X-Test-User; the extension is
itself, with its secret."""

import base64
import hashlib
import json
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from apps.cloud import agent_types, browser_links
from apps.cloud.agent_types import ANDROID_ID, CHROME_ID, LINUX_ID, OSX_ID, WINDOWS_ID
from apps.cloud.browser_links import Tries, clean_code, handle_of
from apps.cloud.packs.android import AndroidPack
from apps.cloud.packs.computer import ComputerPack
from tests.cloud_agent_fixtures import _agent, _bytes, _client, _give, _put_skill_file, _revision, me  # noqa: F401 — fixtures
from tests.conftest import make_client, needs_db

BASE = "/api/v1/browser-links"
# Short waits, so the tests of what runs out of time do not take a minute.
FAST = {"browser_link_claim_seconds": 0.4, "browser_link_call_min_seconds": 0.1}


def secret() -> str:
    return base64.urlsafe_b64encode(uuid.uuid4().bytes + uuid.uuid4().bytes).decode().rstrip("=")


def ext(s: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {s}"}


class Extension:
    """One browser's extension: its secret, and the link it started."""

    def __init__(self, client: TestClient, name: str = "Scout", **body) -> None:
        self.client, self.secret = client, secret()
        # The hash is of the secret's text, exactly as it is sent after "Bearer ".
        r = client.post(f"{BASE}/start", json={"name": name, "secret_hash": hashlib.sha256(self.secret.encode()).hexdigest(), "browser": "Chrome", "extension_version": "1.0.0", **body})
        assert r.status_code == 200, r.text
        self.id, self.code = r.json()["id"], r.json()["code"]
        self.started = r.json()

    def poll(self, wait: float = 0, **status):
        return self.client.post(f"{BASE}/{self.id}/poll?wait={wait}", json={"status": status}, headers=ext(self.secret))

    def result(self, call_id: str, **body):
        return self.client.post(f"{BASE}/{self.id}/calls/{call_id}/result", json=body, headers=ext(self.secret))

    def unpair(self):
        return self.client.delete(f"{BASE}/{self.id}", headers=ext(self.secret))


@pytest.fixture
def client(ui_dist: Path, migrated_db_url: str, tmp_path: Path) -> Iterator[TestClient]:
    c = _client(ui_dist, migrated_db_url, tmp_path, browser_link_starts_per_minute=10_000, **FAST)
    yield c
    c.__exit__(None, None, None)
    # Links nobody claimed are nobody's: the tests' own leftovers go by their look.
    with create_engine(migrated_db_url).begin() as conn:
        conn.execute(text("DELETE FROM browser_links WHERE user_id IS NULL"))


def paired(client: TestClient, me: dict[str, str], name: str = "Scout", **claim) -> Extension:
    e = Extension(client, name)
    r = client.post(f"{BASE}/claim", json={"code": e.code, **claim}, headers=me)
    assert r.status_code == 200, r.text
    e.claimed = r.json()
    assert e.poll().json()["state"] == "paired"
    return e


def refusal(r) -> tuple[int, str]:
    assert isinstance(r.json()["detail"], str) and r.json()["detail"], r.text
    return r.status_code, r.json()["code"]


def agents_of(client: TestClient, me: dict[str, str]) -> list[dict]:
    return [a for a in client.get("/api/v1/agents", headers=me).json()["agents"] if a["name"] != "wysebee"]


def agent_item(client: TestClient, me: dict[str, str], name: str) -> dict:
    """The person's agent named ``name``, as GET /api/v1/agents answers it."""
    return next(a for a in client.get("/api/v1/agents", headers=me).json()["agents"] if a["name"] == name)


def agent_config(client: TestClient, me: dict[str, str], name: str) -> dict:
    return agent_item(client, me, name)["config"]


def delete_agent(client: TestClient, who: dict[str, str], name: str):
    return client.delete(f"/api/v1/agents/{agent_item(client, who, name)['id']}", headers=who)


# --- no database -----------------------------------------------------------------------------


def test_a_handle_is_made_from_the_typed_name() -> None:
    assert handle_of("Scout") == "scout"
    assert handle_of("  My Work Browser!  ") == "my-work-browser"
    assert handle_of("Café — Zoë's") == "caf-zo-s"
    assert handle_of("a" * 40) == "a" * 30
    assert handle_of("abcdefghijklmnopqrstuvwxyz012 x") == "abcdefghijklmnopqrstuvwxyz012"  # no dash left at the cut
    for none in ("", "123", "9 lives", "日本語", "---"):
        assert handle_of(none) is None, none


def test_a_code_is_read_with_or_without_the_dash_in_any_case() -> None:
    assert clean_code("abcd-efgh") == clean_code(" ABCD EFGH ") == clean_code("abcdefgh") == "ABCDEFGH"
    for bad in (None, 12345678, "", "ABCD-EFG", "ABCD-EFGHJ", "ABCD-EFG0", "ABCD-EFGI", "ABCD_EFGH"):
        assert clean_code(bad) is None, bad


def test_wrong_codes_are_counted_in_a_window() -> None:
    tries = Tries(2, 600)
    assert tries.allowed("a", 0)
    tries.failure("a", 0)
    tries.failure("a", 10)
    assert not tries.allowed("a", 20) and tries.allowed("b", 20)
    assert not tries.allowed("a", 599) and tries.allowed("a", 600)  # the first fell out of the window
    assert tries.allowed("a", 5000) and "a" not in tries.failed


def test_without_sign_in_the_persons_routes_are_refused_and_start_checks_its_body(ui_dist: Path) -> None:
    with make_client(ui_dist, auth_issuer="https://tenant.example.auth0.com/", browser_link_starts_per_minute=1000) as c:
        assert c.get(BASE).status_code == 401
        assert c.post(f"{BASE}/claim", json={"code": "ABCDEFGH"}).status_code == 401
        assert c.post(f"{BASE}/{'0' * 32}/calls", json={"op": "ping"}).status_code == 401
        # Checked before the database is touched (it is unreachable here).
        good = "0" * 64
        for body in ({"secret_hash": good}, {"name": " ", "secret_hash": good}, {"name": "x" * 61, "secret_hash": good}, {"name": "two\nlines", "secret_hash": good}, {"name": 5, "secret_hash": good}):
            assert refusal(c.post(f"{BASE}/start", json=body)) == (422, "invalid_name"), body
        for bad in (None, "abc", "G" * 64, "A" * 64, 5):
            assert refusal(c.post(f"{BASE}/start", json={"name": "Scout", "secret_hash": bad})) == (422, "invalid_hash"), bad
        assert refusal(c.post(f"{BASE}/start", content=b"[1]")) == (422, "invalid_body")
        assert refusal(c.post(f"{BASE}/start", content=b"{not json")) == (422, "invalid_body")
        assert refusal(c.post(f"{BASE}/start", json={"name": "x" * 5000, "secret_hash": good})) == (413, "too_big")
        # Sent in chunks with no length said: read no further than the limit.
        chunked = iter([b'{"name": "', b"x" * 5000, b'"}'])
        assert refusal(c.post(f"{BASE}/start", content=chunked)) == (413, "too_big")


def test_starts_from_one_address_are_limited(ui_dist: Path) -> None:
    with make_client(ui_dist, browser_link_starts_per_minute=2) as c:
        here = {"X-Forwarded-For": "203.0.113.9, 198.51.100.7"}
        for _ in range(2):
            assert refusal(c.post(f"{BASE}/start", json={}, headers=here)) == (422, "invalid_name")
        assert refusal(c.post(f"{BASE}/start", json={}, headers=here)) == (429, "too_many_starts")
        # The address the ingress saw is the last hop; another one is not held back.
        assert refusal(c.post(f"{BASE}/start", json={}, headers={"X-Forwarded-For": "198.51.100.7, 203.0.113.10"})) == (422, "invalid_name")
        assert refusal(c.post(f"{BASE}/start", json={})) == (422, "invalid_name")


# --- pairing -----------------------------------------------------------------------------------


@needs_db
def test_an_extension_starts_shows_a_code_and_waits(client: TestClient, migrated_db_url: str) -> None:
    e = Extension(client, "  Scout  ")
    assert e.started["expires_in"] == 600 and len(e.id) == 32
    assert len(e.code) == 8 and all(c in browser_links.CODE_ALPHABET for c in e.code)
    waiting = e.poll().json()
    assert waiting["state"] == "pending" and 590 <= waiting["expires_in"] <= 600
    # Only the secret's hash is kept; the typed name trimmed.
    with create_engine(migrated_db_url).connect() as conn:
        row = conn.execute(text("SELECT user_id, secret_hash, name, browser, extension_version FROM browser_links WHERE id = :i"), {"i": e.id}).one()
    assert tuple(row) == (None, hashlib.sha256(e.secret.encode()).hexdigest(), "Scout", "Chrome", "1.0.0")
    # Another secret, no secret, or a link that is not there: unpaired.
    for headers in (ext(secret()), {}, {"Authorization": f"Basic {e.secret}"}):
        assert refusal(client.post(f"{BASE}/{e.id}/poll", json={}, headers=headers)) == (401, "unpaired")
    assert refusal(client.post(f"{BASE}/{'0' * 32}/poll", json={}, headers=ext(e.secret))) == (401, "unpaired")
    assert refusal(client.post(f"{BASE}/{e.id}/calls/{'0' * 32}/result", json={"ok": True}, headers=ext(secret()))) == (401, "unpaired")
    assert refusal(client.delete(f"{BASE}/{e.id}", headers=ext(secret()))) == (401, "unpaired")
    # The person cancelled before typing the code: the pending link goes.
    gone = e.unpair()
    assert (gone.status_code, gone.content) == (204, b"")
    assert refusal(e.poll()) == (401, "unpaired")


@needs_db
def test_a_code_nobody_typed_runs_out(ui_dist: Path, migrated_db_url: str, tmp_path: Path, me: dict[str, str]) -> None:
    c = _client(ui_dist, migrated_db_url, tmp_path, browser_link_code_seconds=0.3)
    try:
        e = Extension(c)
        # A waiting poll answers when the code runs out, not after its whole wait.
        started = time.monotonic()
        assert refusal(e.poll(wait=20)) == (410, "expired")
        assert time.monotonic() - started < 5
        assert refusal(e.poll()) == (401, "unpaired")
        late = Extension(c)
        time.sleep(0.4)
        assert refusal(c.post(f"{BASE}/claim", json={"code": late.code}, headers=me)) == (404, "code_unknown")
        # Purged a code's time after it ran out, had its extension never asked again.
        time.sleep(0.4)
        c.portal.call(_purge, c.app.state)
        assert refusal(late.poll()) == (401, "unpaired")
    finally:
        c.__exit__(None, None, None)


async def _purge(state) -> int:
    async with state.db.sessionmaker() as session:
        return await browser_links.purge(session, state.settings)


@needs_db
def test_one_address_holds_only_so_many_waiting_codes(ui_dist: Path, migrated_db_url: str, tmp_path: Path, me: dict[str, str]) -> None:
    c = _client(ui_dist, migrated_db_url, tmp_path, browser_link_pending_per_address=2, browser_link_starts_per_minute=10_000)
    here, there = {"X-Forwarded-For": "203.0.113.20"}, {"X-Forwarded-For": "203.0.113.21"}
    body = {"name": "Scout", "secret_hash": "0" * 64}
    try:
        first, second = (c.post(f"{BASE}/start", json=body, headers=here).json() for _ in range(2))
        assert refusal(c.post(f"{BASE}/start", json=body, headers=here)) == (429, "too_many_starts")
        # Another address is not held back by this one.
        assert c.post(f"{BASE}/start", json=body, headers=there).status_code == 200
        # Only the address's hash is kept, and only while the code waits: claimed, it is
        # forgotten, and the address may start another.
        with create_engine(migrated_db_url).connect() as conn:
            kept = conn.execute(text("SELECT start_address FROM browser_links WHERE id = :i"), {"i": first["id"]}).scalar_one()
        assert kept == hashlib.sha256(b"203.0.113.20").hexdigest()
        assert c.post(f"{BASE}/claim", json={"code": first["code"]}, headers=me).status_code == 200
        with create_engine(migrated_db_url).connect() as conn:
            assert conn.execute(text("SELECT start_address FROM browser_links WHERE id = :i"), {"i": first["id"]}).scalar_one() is None
        assert c.post(f"{BASE}/start", json=body, headers=here).status_code == 200
        assert second["code"]
    finally:
        c.__exit__(None, None, None)
        with create_engine(migrated_db_url).begin() as conn:
            conn.execute(text("DELETE FROM browser_links WHERE user_id IS NULL"))


@needs_db
def test_no_more_links_may_wait_than_the_cap(ui_dist: Path, migrated_db_url: str, tmp_path: Path) -> None:
    c = _client(ui_dist, migrated_db_url, tmp_path, browser_link_pending_max=0)
    try:
        assert refusal(c.post(f"{BASE}/start", json={"name": "Scout", "secret_hash": "0" * 64})) == (503, "busy")
    finally:
        c.__exit__(None, None, None)


@needs_db
def test_typing_the_code_pairs_the_browser_and_makes_its_agent(client: TestClient, me: dict[str, str]) -> None:
    e = Extension(client, "My Work Browser")
    assert refusal(client.post(f"{BASE}/claim", json={"code": "ZZZZ-ZZZZ"}, headers=me)) == (404, "code_unknown")
    assert refusal(client.post(f"{BASE}/claim", json={}, headers=me)) == (404, "code_unknown")
    before = _revision(client, me)
    typed = f"{e.code[:4]}-{e.code[4:]}".lower()
    r = client.post(f"{BASE}/claim", json={"code": typed}, headers=me)
    assert r.status_code == 200, r.text
    link = r.json()["link"]
    assert r.json()["agent"] == {"name": "my-work-browser", "label": "My Work Browser"}
    assert {k: link[k] for k in ("id", "agent", "label", "browser", "extension_version", "status", "paused", "last_seen")} == {
        "id": e.id, "agent": "my-work-browser", "label": "My Work Browser", "browser": "Chrome", "extension_version": "1.0.0", "status": "offline", "paused": False, "last_seen": None}
    assert link["created_at"].endswith("Z")
    # The agent is one of the person's agents, and the change feed heard of it.
    assert _revision(client, me) > before
    (item,) = agents_of(client, me)
    assert (item["name"], item["description"], link["agent_id"]) == ("my-work-browser", None, item["id"])
    assert str(uuid.UUID(item["id"])) == item["id"]
    assert item["config"] == {"label": "My Work Browser", "browser_access": True, "browser_link": e.id, "paired_kind": "wysebee-browser-chrome", "agent_type_id": CHROME_ID}
    # The extension hears, and from then on is connected; it says how it is.
    answer = e.poll(paused=True, extension_version="1.0.1", browser="Edge").json()
    assert answer == {"state": "paired", "agent": {"name": "my-work-browser", "label": "My Work Browser"}, "calls": [], "cancelled": []}
    for path in (BASE, f"{BASE}/"):
        got = client.get(path, headers=me, follow_redirects=False)
        assert got.status_code == 200, path
        (mine,) = got.json()["links"]
        assert (mine["id"], mine["status"], mine["paused"], mine["extension_version"], mine["browser"]) == (e.id, "connected", True, "1.0.1", "Edge")
        assert mine["last_seen"].endswith("Z")
    # A code works once; another account sees nothing of it.
    assert refusal(client.post(f"{BASE}/claim", json={"code": e.code}, headers=me)) == (404, "code_unknown")
    other = {"X-Test-User": f"test-{uuid.uuid4().hex}"}
    assert client.get(BASE, headers=other).json() == {"links": []}
    assert refusal(client.post(f"{BASE}/{e.id}/unpair", headers=other)) == (404, "browser_unpaired")
    assert refusal(client.post(f"{BASE}/{e.id}/calls", json={"op": "ping"}, headers=other)) == (404, "browser_unpaired")


@needs_db
def test_a_browsers_agent_is_made_whatever_else_the_claim_says(client: TestClient, me: dict[str, str]) -> None:
    # A home an old client sends is not kept: every agent lives in Wysebee Cloud.
    cloud = {"paired_kind": "wysebee-browser-chrome", "agent_type_id": CHROME_ID}
    named = paired(client, me, "Alpha", home="desk-1", home_name=" Sam's Mac ")
    assert agent_config(client, me, "alpha") == {"label": "Alpha", "browser_access": True, "browser_link": named.id, **cloud}
    known = paired(client, me, "Beta", home="desk-2")
    assert agent_config(client, me, "beta") == {"label": "Beta", "browser_access": True, "browser_link": known.id, **cloud}
    plain = paired(client, me, "Gamma")
    assert agent_config(client, me, "gamma") == {"label": "Gamma", "browser_access": True, "browser_link": plain.id, **cloud}
    assert {link["agent"] for link in client.get(BASE, headers=me).json()["links"]} == {"alpha", "beta", "gamma"}
    assert known.id != named.id


@needs_db
def test_a_claim_refused_for_its_name_keeps_the_code_good(client: TestClient, me: dict[str, str], migrated_db_url: str) -> None:
    # 2026-11-13: the person settles the name in Wysebee, with the same code; the extension
    # goes on showing it.
    _agent(client, me, "sky", {"label": "Sky"})
    for name, status, reason in (("123", 422, "invalid_name"), ("Pairing", 422, "reserved"), ("camera!", 422, "reserved"), ("Sky", 409, "name_taken"), ("Wysebee", 409, "name_taken")):
        e = Extension(client, name)
        r = client.post(f"{BASE}/claim", json={"code": e.code}, headers=me)
        assert refusal(r) == (status, reason), name
        assert e.poll().json()["state"] == "pending", name
        if reason == "name_taken":
            # It says whose the name is; neither a plain agent nor Wysebee can be reconnected.
            label = "Sky" if name == "Sky" else "Wysebee"
            assert {k: r.json()[k] for k in ("agent", "reconnect")} == {"agent": {"name": name.lower(), "label": label}, "reconnect": False}
            assert refusal(client.post(f"{BASE}/claim", json={"code": e.code, "reconnect": True}, headers=me)) == (409, "name_taken")
        # Another name the start's rule refuses is refused the same way, and the code still works.
        for bad in ("", "   ", "x" * 61, "two\nlines", 7):
            assert refusal(client.post(f"{BASE}/claim", json={"code": e.code, "name": bad}, headers=me)) == (422, "invalid_name"), bad
        assert refusal(client.post(f"{BASE}/claim", json={"code": e.code, "name": "Pairing"}, headers=me)) == (422, "reserved")
        assert e.poll().json()["state"] == "pending"
    # A name of the person's own: the agent is made under it, and the extension hears it.
    e = Extension(client, "Sky")
    r = client.post(f"{BASE}/claim", json={"code": e.code, "name": " Sky 2 "}, headers=me)
    assert r.status_code == 200, r.text
    assert r.json()["agent"] == {"name": "sky-2", "label": "Sky 2"}
    assert agent_config(client, me, "sky-2")["label"] == "Sky 2"
    assert e.poll().json()["agent"] == {"name": "sky-2", "label": "Sky 2"}
    assert next(link for link in client.get(BASE, headers=me).json()["links"] if link["id"] == e.id)["label"] == "Sky 2"
    # A reconnect asked for a name nobody has makes the agent as any claim does.
    e = Extension(client, "Fresh")
    assert client.post(f"{BASE}/claim", json={"code": e.code, "reconnect": True}, headers=me).json()["agent"] == {"name": "fresh", "label": "Fresh"}
    # Twenty agents at most, Wysebee not counted; the code waits while one is deleted.
    _agent(client, me, "wysebee", {"label": "Wysebee"})
    for i in range(17):
        _agent(client, me, f"a{i}", {"label": "A"})
    e = Extension(client, "One More")
    assert refusal(client.post(f"{BASE}/claim", json={"code": e.code}, headers=me)) == (409, "too_many")
    assert e.poll().json()["state"] == "pending"
    assert delete_agent(client, me, "a0").status_code == 204
    assert client.post(f"{BASE}/claim", json={"code": e.code}, headers=me).json()["agent"]["name"] == "one-more"
    assert e.poll().json()["state"] == "paired"
    # A link an older cloud refused is still told so, and its code is gone.
    old = Extension(client, "Old")
    with create_engine(migrated_db_url).begin() as conn:
        conn.execute(text("UPDATE browser_links SET refused = 'name_taken', code = NULL WHERE id = :id"), {"id": old.id})
    told = old.poll().json()
    assert (told["state"], told["reason"]) == ("refused", "name_taken") and told["message"]
    assert refusal(client.post(f"{BASE}/claim", json={"code": old.code}, headers=me)) == (404, "code_unknown")


@needs_db
def test_a_name_clash_only_the_save_sees_refuses_the_claim_and_makes_nothing(client: TestClient, me: dict[str, str], monkeypatch) -> None:
    from fastapi import HTTPException

    from apps.cloud import agents

    async def clash(*a, **k):
        # What the name index answers for a clash the claim's own check could not see.
        raise HTTPException(status_code=409, detail="There is already an agent named 'scout'.")

    monkeypatch.setattr(agents, "save", clash)
    e = Extension(client, "Scout")
    r = client.post(f"{BASE}/claim", json={"code": e.code}, headers=me)
    assert refusal(r) == (409, "name_taken")
    # Nothing can be said of an agent the claim could not see: it cannot be reconnected.
    assert (r.json()["agent"], r.json()["reconnect"]) == ({"name": "scout", "label": "scout"}, False)
    assert e.poll().json()["state"] == "pending"
    # Nothing of it kept: no link for the person, no agent; and the code still works.
    assert client.get(BASE, headers=me).json()["links"] == []
    assert agents_of(client, me) == []
    monkeypatch.undo()
    assert client.post(f"{BASE}/claim", json={"code": e.code}, headers=me).json()["agent"]["name"] == "scout"


@needs_db
def test_ten_wrong_codes_and_the_account_waits(client: TestClient, me: dict[str, str]) -> None:
    e = Extension(client)
    for _ in range(10):
        assert refusal(client.post(f"{BASE}/claim", json={"code": "ZZZZZZZZ"}, headers=me)) == (404, "code_unknown")
    assert refusal(client.post(f"{BASE}/claim", json={"code": e.code}, headers=me)) == (429, "too_many_tries")
    # Another account is not held back, and the link was not refused.
    other = {"X-Test-User": f"test-{uuid.uuid4().hex}"}
    try:
        assert client.post(f"{BASE}/claim", json={"code": e.code}, headers=other).status_code == 200
    finally:
        assert client.post(f"{BASE}/{e.id}/unpair", headers=other).status_code == 204
        delete_agent(client, other, "scout")


@needs_db
def test_a_waiting_poll_answers_as_soon_as_the_code_is_typed(client: TestClient, me: dict[str, str]) -> None:
    e = Extension(client)
    answers: list = []
    waiting = threading.Thread(target=lambda: answers.append(e.poll(wait=20).json()))
    started = time.monotonic()
    waiting.start()
    time.sleep(0.3)
    assert client.post(f"{BASE}/claim", json={"code": e.code}, headers=me).status_code == 200
    waiting.join(timeout=10)
    assert answers and answers[0]["state"] == "paired" and time.monotonic() - started < 5


# --- driving -----------------------------------------------------------------------------------


@needs_db
def test_a_call_goes_to_the_extension_and_its_result_comes_back(client: TestClient, me: dict[str, str]) -> None:
    e = paired(client, me)
    for body, code in (({"op": "configure"}, "invalid_op"), ({}, "invalid_op"), ({"op": "ping", "args": [1]}, "invalid_args"),
                       ({"op": "ping", "timeout_seconds": 0.05}, "invalid_timeout"), ({"op": "ping", "timeout_seconds": True}, "invalid_timeout"), ({"op": "ping", "timeout_seconds": 241}, "invalid_timeout"), ({"op": "ping", "timeout_seconds": "60"}, "invalid_timeout")):
        assert refusal(client.post(f"{BASE}/{e.id}/calls", json=body, headers=me)) == (422, code), body
    assert refusal(client.post(f"{BASE}/{e.id}/calls", json={"op": "ping", "args": {"x": "y" * 200_000}}, headers=me)) == (413, "too_big")
    assert refusal(client.post(f"{BASE}/{'0' * 32}/calls", json={"op": "ping"}, headers=me)) == (404, "browser_unpaired")

    sent = client.post(f"{BASE}/{e.id}/calls", json={"op": "browser_click", "args": {"ref": "e7", "tab_id": None}}, headers=me).json()
    assert set(sent) == {"id", "status"} and sent["status"] == "waiting"
    assert client.get(f"{BASE}/calls/{sent['id']}", headers=me).json() == sent
    # Another account's call is not there, and cannot be stopped.
    other = {"X-Test-User": f"test-{uuid.uuid4().hex}"}
    assert refusal(client.get(f"{BASE}/calls/{sent['id']}", headers=other)) == (404, "call_gone")
    assert client.post(f"{BASE}/calls/{sent['id']}/cancel", headers=other).status_code == 204
    # Handed out once.
    work = e.poll().json()
    assert work["calls"] == [{"id": sent["id"], "op": "browser_click", "args": {"ref": "e7", "tab_id": None}}] and work["cancelled"] == []
    assert e.poll().json()["calls"] == []
    assert client.get(f"{BASE}/calls/{sent['id']}", headers=me).json() == {"id": sent["id"], "status": "running"}
    # Only its own extension answers it.
    stranger = paired(client, me, "Stranger")
    assert refusal(stranger.result(sent["id"], ok=True, result={})) == (404, "call_gone")
    done = e.result(sent["id"], ok=True, result={"clicked": True, "text": "nul\x00led"})
    assert (done.status_code, done.content) == (204, b"")
    assert client.get(f"{BASE}/calls/{sent['id']}", headers=me).json() == {"id": sent["id"], "status": "done", "ok": True, "result": {"clicked": True, "text": "nulled"}}
    # Read once, then forgotten; a second answer is dropped.
    assert refusal(client.get(f"{BASE}/calls/{sent['id']}", headers=me)) == (404, "call_gone")
    assert refusal(e.result(sent["id"], ok=True, result={})) == (404, "call_gone")

    # The extension refusing or failing the op is its answer: done, not ok.
    for answer, error in (({"ok": False, "error": "The person said no."}, "The person said no."), ({"ok": False}, "The browser refused this, without saying why.")):
        sent = client.post(f"{BASE}/{e.id}/calls", json={"op": "browser_tabs"}, headers=me).json()
        assert [c["args"] for c in e.poll().json()["calls"]] == [{}]
        assert e.result(sent["id"], **answer).status_code == 204
        assert client.get(f"{BASE}/calls/{sent['id']}", headers=me).json() == {"id": sent["id"], "status": "done", "ok": False, "error": error}


@needs_db
def test_a_waiting_driver_is_answered_when_the_extension_is(client: TestClient, me: dict[str, str]) -> None:
    e = paired(client, me)

    def answer() -> None:
        work = e.poll(wait=10).json()
        for call in work["calls"]:
            e.result(call["id"], ok=True, result={"op": call["op"]})

    extension = threading.Thread(target=answer)
    extension.start()
    started = time.monotonic()
    done = client.post(f"{BASE}/{e.id}/calls?wait=20", json={"op": "browser_tabs"}, headers=me).json()
    extension.join(timeout=10)
    assert (done["status"], done["ok"], done["result"]) == ("done", True, {"op": "browser_tabs"})
    assert time.monotonic() - started < 5


@needs_db
def test_a_browser_that_is_not_there_is_said_at_once_or_soon(ui_dist: Path, migrated_db_url: str, tmp_path: Path, me: dict[str, str]) -> None:
    c = _client(ui_dist, migrated_db_url, tmp_path, browser_link_online_seconds=0.5, **FAST)
    try:
        e = Extension(c)
        assert c.post(f"{BASE}/claim", json={"code": e.code}, headers=me).status_code == 200
        # Never polled since it was paired, then not polled for a while: offline, at once.
        assert refusal(c.post(f"{BASE}/{e.id}/calls", json={"op": "ping"}, headers=me)) == (409, "browser_offline")
        # Switched off in the extension: its last poll says so, and a call is refused at once,
        # though it polled a moment ago.
        e.poll(paused=True)
        assert refusal(c.post(f"{BASE}/{e.id}/calls", json={"op": "ping"}, headers=me)) == (409, "browser_offline")
        # A long poll sent before it was switched off, still waiting, does not switch it back on.
        waiting = threading.Thread(target=lambda: e.poll(wait=0.4, paused=False))
        waiting.start()
        time.sleep(0.1)
        e.poll(paused=True)
        waiting.join()
        assert c.get(BASE, headers=me).json()["links"][0]["paused"] is True
        # Switched back on: its next poll says not paused.
        e.poll(paused=False)
        assert c.get(BASE, headers=me).json()["links"][0]["status"] == "connected"
        time.sleep(0.6)
        assert c.get(BASE, headers=me).json()["links"][0]["status"] == "offline"
        assert refusal(c.post(f"{BASE}/{e.id}/calls", json={"op": "ping"}, headers=me)) == (409, "browser_offline")
        # Connected, but it does not take the call in time: failed, saying so — the waiting
        # driver is answered then, not after its whole wait.
        e.poll()
        started = time.monotonic()
        failed = c.post(f"{BASE}/{e.id}/calls?wait=20", json={"op": "ping"}, headers=me).json()
        assert time.monotonic() - started < 5
        assert failed == {"id": failed["id"], "status": "failed", "ok": False, "error": browser_links.NOT_REACHED}
        assert refusal(c.get(f"{BASE}/calls/{failed['id']}", headers=me)) == (404, "call_gone")
        assert e.poll().json()["calls"] == []
    finally:
        c.__exit__(None, None, None)


@needs_db
def test_a_browsers_agent_is_offline_while_its_browser_is(ui_dist: Path, migrated_db_url: str, tmp_path: Path, me: dict[str, str]) -> None:
    """It lives in its browser (2026-11-13): offline while the extension does not poll, or is
    paused, as its Connect AI app tab says — and online once it polls again."""
    c = _client(ui_dist, migrated_db_url, tmp_path, browser_link_online_seconds=0.5, **FAST)
    try:
        e = Extension(c, "Shelf")
        assert c.post(f"{BASE}/claim", json={"code": e.code}, headers=me).status_code == 200
        offline = lambda: c.get("/api/v1/mcp/status?agent=shelf", headers=me).json()["offline"]
        # Never polled: offline, in words naming the browser.
        assert offline().startswith("@shelf is offline: it lives in Chrome")
        # Paused in the extension: offline too.
        e.poll(paused=True)
        assert "paused" in offline()
        # Open and on: online.
        e.poll(paused=False)
        assert offline() is None
        time.sleep(0.6)
        assert offline() is not None
    finally:
        c.__exit__(None, None, None)


@needs_db
def test_a_call_not_answered_in_its_time_fails_and_the_extension_is_told_to_stop(client: TestClient, me: dict[str, str]) -> None:
    e = paired(client, me)
    sent = client.post(f"{BASE}/{e.id}/calls", json={"op": "browser_click", "args": {"ref": "e1"}, "timeout_seconds": 0.5}, headers=me).json()
    assert [c["id"] for c in e.poll().json()["calls"]] == [sent["id"]]
    failed = client.get(f"{BASE}/calls/{sent['id']}?wait=20", headers=me).json()
    assert (failed["status"], failed["ok"]) == ("failed", False) and "approve" in failed["error"]
    assert refusal(client.get(f"{BASE}/calls/{sent['id']}", headers=me)) == (404, "call_gone")
    assert e.poll().json() == {"state": "paired", "agent": {"name": "scout", "label": "Scout"}, "calls": [], "cancelled": [sent["id"]]}
    assert e.poll().json()["cancelled"] == []
    assert refusal(e.result(sent["id"], ok=True, result={})) == (404, "call_gone")
    # The same when the extension hears before the driver reads.
    sent = client.post(f"{BASE}/{e.id}/calls", json={"op": "ping", "timeout_seconds": 0.3}, headers=me).json()
    e.poll()
    time.sleep(0.4)
    assert e.poll().json()["cancelled"] == [sent["id"]]
    assert client.get(f"{BASE}/calls/{sent['id']}", headers=me).json()["status"] == "failed"
    assert refusal(client.get(f"{BASE}/calls/{sent['id']}", headers=me)) == (404, "call_gone")


@needs_db
def test_a_cancelled_call_stops_in_the_extension(client: TestClient, me: dict[str, str]) -> None:
    e = paired(client, me)
    # Not taken yet: it never reaches the extension.
    sent = client.post(f"{BASE}/{e.id}/calls", json={"op": "ping"}, headers=me).json()
    stopped = client.post(f"{BASE}/calls/{sent['id']}/cancel", headers=me)
    assert (stopped.status_code, stopped.content) == (204, b"")
    assert refusal(client.get(f"{BASE}/calls/{sent['id']}", headers=me)) == (404, "call_gone")
    assert e.poll().json() == {"state": "paired", "agent": {"name": "scout", "label": "Scout"}, "calls": [], "cancelled": []}
    # Taken: the extension is told on its next poll, once, and its late answer is dropped.
    sent = client.post(f"{BASE}/{e.id}/calls", json={"op": "browser_navigate", "args": {"url": "https://example.com"}}, headers=me).json()
    e.poll()
    assert client.post(f"{BASE}/calls/{sent['id']}/cancel", headers=me).status_code == 204
    # Cancelled again before the extension heard: it is still told, once.
    assert client.post(f"{BASE}/calls/{sent['id']}/cancel", headers=me).status_code == 204
    assert refusal(client.get(f"{BASE}/calls/{sent['id']}", headers=me)) == (404, "call_gone")
    assert e.poll().json()["cancelled"] == [sent["id"]]
    assert e.poll().json()["cancelled"] == []
    assert refusal(e.result(sent["id"], ok=True, result={})) == (404, "call_gone")
    # Finished, or gone: nothing to stop.
    sent = client.post(f"{BASE}/{e.id}/calls", json={"op": "ping"}, headers=me).json()
    e.poll()
    e.result(sent["id"], ok=True, result="pong")
    assert client.post(f"{BASE}/calls/{sent['id']}/cancel", headers=me).status_code == 204
    assert client.post(f"{BASE}/calls/{sent['id']}/cancel", headers=me).status_code == 204
    assert e.poll().json()["cancelled"] == []


@needs_db
def test_a_result_for_no_call_of_its_own_is_refused_before_its_body_is_read(ui_dist: Path, migrated_db_url: str, tmp_path: Path, me: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    read: list[int] = []
    real_body = browser_links._body

    async def counted(request, max_bytes):
        read.append(max_bytes)
        return await real_body(request, max_bytes)

    monkeypatch.setattr(browser_links, "_body", counted)
    c = _client(ui_dist, migrated_db_url, tmp_path, browser_link_result_max_bytes=2000, browser_link_starts_per_minute=10_000)
    try:
        big = {"ok": True, "result": {"image": "x" * 5000}}
        # A link nobody claimed has no calls: a 404, not the 413 its body would earn.
        waiting = Extension(c, "Waiting")
        assert refusal(waiting.result(uuid.uuid4().hex, **big)) == (404, "call_gone")
        # A paired one: only a call it took, and has not answered yet.
        e = paired(c, me)
        assert refusal(e.result(uuid.uuid4().hex, **big)) == (404, "call_gone")
        sent = c.post(f"{BASE}/{e.id}/calls", json={"op": "ping"}, headers=me).json()
        assert refusal(e.result(sent["id"], **big)) == (404, "call_gone")  # not taken yet
        assert 2000 not in read  # none of those bodies was read
        e.poll()
        assert e.result(sent["id"], ok=True, result={}).status_code == 204
        assert read.count(2000) == 1  # the one answer to a call it took
        assert refusal(e.result(sent["id"], **big)) == (404, "call_gone")  # answered already
        assert read.count(2000) == 1
    finally:
        c.__exit__(None, None, None)
        with create_engine(migrated_db_url).begin() as conn:
            conn.execute(text("DELETE FROM browser_links WHERE user_id IS NULL"))


@needs_db
def test_a_result_too_big_fails_the_call(ui_dist: Path, migrated_db_url: str, tmp_path: Path, me: dict[str, str]) -> None:
    c = _client(ui_dist, migrated_db_url, tmp_path, browser_link_result_max_bytes=2000)
    try:
        e = paired(c, me)
        sent = c.post(f"{BASE}/{e.id}/calls", json={"op": "browser_screenshot"}, headers=me).json()
        e.poll()
        assert refusal(e.result(sent["id"], ok=True, result={"image": "x" * 5000})) == (413, "too_big")
        assert c.get(f"{BASE}/calls/{sent['id']}", headers=me).json() == {"id": sent["id"], "status": "failed", "ok": False, "error": browser_links.TOO_BIG}
    finally:
        c.__exit__(None, None, None)


@needs_db
def test_a_result_for_a_call_that_ended_while_it_was_read_is_dropped(client: TestClient, me: dict[str, str], migrated_db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    e = paired(client, me)
    sent = client.post(f"{BASE}/{e.id}/calls", json={"op": "ping"}, headers=me).json()
    e.poll()
    real_body = browser_links._body

    async def slow_answer(request, max_bytes):
        body = await real_body(request, max_bytes)
        # The call runs out of time while its answer is still arriving.
        with create_engine(migrated_db_url).begin() as conn:
            conn.execute(text("UPDATE browser_calls SET status = 'failed', ok = false, error = 'Out of time.', finished_at = now() WHERE id = :i"), {"i": sent["id"]})
        return body

    monkeypatch.setattr(browser_links, "_body", slow_answer)
    assert refusal(e.result(sent["id"], ok=True, result={"pong": True})) == (404, "call_gone")
    # The call keeps how it ended; the late answer is not put in its place.
    assert client.get(f"{BASE}/calls/{sent['id']}", headers=me).json() == {"id": sent["id"], "status": "failed", "ok": False, "error": "Out of time."}


@needs_db
def test_calls_are_kept_an_hour_at_most(client: TestClient, me: dict[str, str], migrated_db_url: str) -> None:
    e = paired(client, me)
    old, older = (client.post(f"{BASE}/{e.id}/calls", json={"op": "ping"}, headers=me).json()["id"] for _ in range(2))
    with create_engine(migrated_db_url).begin() as conn:
        conn.execute(text("UPDATE browser_calls SET created_at = now() - interval '2 hours' WHERE id = :i"), {"i": old})
    assert client.portal.call(_purge, client.app.state) == 1
    assert refusal(client.get(f"{BASE}/calls/{old}", headers=me)) == (404, "call_gone")
    # …and the extension's own poll drops its link's old ones too.
    with create_engine(migrated_db_url).begin() as conn:
        conn.execute(text("UPDATE browser_calls SET created_at = now() - interval '2 hours', status = 'done' WHERE id = :i"), {"i": older})
    e.poll()
    assert refusal(client.get(f"{BASE}/calls/{older}", headers=me)) == (404, "call_gone")


# --- the cloud's own loop ----------------------------------------------------------------------


class Browser:
    """The extension at work in a thread: polls, and answers each call with ``answer``."""

    def __init__(self, e: Extension, answer) -> None:
        self.e, self.answer, self.seen, self.cancelled = e, answer, [], []
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            r = self.e.poll(wait=1)
            if r.status_code != 200:
                return
            self.cancelled += r.json()["cancelled"]
            for call in r.json()["calls"]:
                self.seen.append(call)
                body = self.answer(call["op"], call["args"])
                if body is not None:
                    self.e.result(call["id"], **body)

    def __enter__(self) -> "Browser":
        self.thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._stop.set()
        self.thread.join(timeout=10)


@needs_db
def test_the_clouds_own_loop_runs_an_op_in_process(client: TestClient, me: dict[str, str]) -> None:
    sub, state = me["X-Test-User"], client.app.state
    e = paired(client, me)
    scout = agent_item(client, me, "scout")["id"]

    def op(*args, **kwargs) -> dict:
        return client.portal.call(lambda: browser_links.run_op(state, sub, *args, **kwargs))

    with Browser(e, lambda op, args: {"ok": True, "result": {"op": op, **args}} if op != "browser_click" else {"ok": False, "error": "Refused by the person."}) as browser:
        assert op(e.id, "browser_tabs", {"x": 1}, agent=scout) == {"ok": True, "result": {"op": "browser_tabs", "x": 1}}
        assert op(e.id, "browser_click", {}, agent=scout) == {"ok": False, "error": "Refused by the person.", "unavailable": False}
        assert [c["op"] for c in browser.seen] == ["browser_tabs", "browser_click"]
        # Only its own agent, in its own account; and only the browser's ops.
        assert op(e.id, "ping", {}, agent="wysebee") == {"ok": False, "error": browser_links.NO_LINK, "unavailable": True}
        assert client.portal.call(lambda: browser_links.run_op(state, "someone-else", e.id, "ping", {})) == {"ok": False, "error": browser_links.NO_LINK, "unavailable": True}
        assert op(e.id, "shutdown", {})["unavailable"] is False
        too_long = {"x": "y" * client.app.state.settings.browser_link_args_max_bytes}
        assert op(e.id, "browser_tabs", too_long, agent=scout) == {"ok": False, "error": f"The arguments are too long: at most {client.app.state.settings.browser_link_args_max_bytes} characters.", "unavailable": False}
        assert len(browser.seen) == 2
    # Connected, but it takes nothing: not reached. It ran out of its own time: not that.
    e.poll()
    assert op(e.id, "ping", {}) == {"ok": False, "error": browser_links.NOT_REACHED, "unavailable": True}
    with Browser(e, lambda op, args: None):
        late = op(e.id, "ping", {}, timeout_seconds=0.5)
    assert late["ok"] is False and late["unavailable"] is False and "approve" in late["error"]


@needs_db
def test_a_stopped_tool_stops_its_call(client: TestClient, me: dict[str, str]) -> None:
    import asyncio

    sub, state = me["X-Test-User"], client.app.state
    e = paired(client, me)

    async def stopped() -> bool:
        task = asyncio.create_task(browser_links.run_op(state, sub, e.id, "browser_navigate", {"url": "https://example.com"}))
        await asyncio.sleep(1.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    with Browser(e, lambda op, args: None) as browser:
        assert client.portal.call(stopped)
        deadline = time.time() + 5
        while not browser.cancelled and time.time() < deadline:
            time.sleep(0.1)
        assert browser.cancelled == [browser.seen[0]["id"]]


@needs_db
def test_a_call_stopped_by_another_while_the_loop_waits_is_an_error_not_a_hang(client: TestClient, me: dict[str, str]) -> None:
    import asyncio

    sub, state = me["X-Test-User"], client.app.state
    e = paired(client, me)
    scout = agent_item(client, me, "scout")["id"]

    with Browser(e, lambda op, args: None) as browser:

        async def go() -> dict:
            task = asyncio.create_task(browser_links.run_op(state, sub, e.id, "browser_navigate", {"url": "https://example.com"}, agent=scout))
            for _ in range(100):
                if browser.seen:
                    break
                await asyncio.sleep(0.05)
            # Stopped elsewhere (the person, another driver): the loop's call is gone.
            await browser_links.cancel_call(state, sub, browser.seen[0]["id"])
            return await asyncio.wait_for(task, 5)

        out = client.portal.call(go)
    assert out == {"ok": False, "error": browser_links.CALL_GONE, "unavailable": False}


# --- unpairing -----------------------------------------------------------------------------------


@needs_db
def test_unpairing_in_wysebee_forgets_the_extension_and_leaves_the_agent(client: TestClient, me: dict[str, str]) -> None:
    e = paired(client, me)
    waiting = client.post(f"{BASE}/{e.id}/calls", json={"op": "ping"}, headers=me).json()
    before = _revision(client, me)
    gone = client.post(f"{BASE}/{e.id}/unpair", headers=me)
    assert (gone.status_code, gone.content) == (204, b"")
    assert refusal(e.poll()) == (401, "unpaired")
    assert client.get(BASE, headers=me).json() == {"links": []}
    # The agent stays, without a browser — a new revision, so every device hears.
    # It keeps the kind it was made for (2026-11-13), to be reconnected.
    assert agent_config(client, me, "scout") == {"label": "Scout", "browser_access": True, "paired_kind": "wysebee-browser-chrome", "agent_type_id": CHROME_ID}
    assert _revision(client, me) == before + 1
    # Its calls went with it, and it is not there to unpair or drive again.
    assert refusal(client.get(f"{BASE}/calls/{waiting['id']}", headers=me)) == (404, "call_gone")
    assert refusal(client.post(f"{BASE}/{e.id}/unpair", headers=me)) == (404, "browser_unpaired")
    assert refusal(client.post(f"{BASE}/{e.id}/calls", json={"op": "ping"}, headers=me)) == (404, "browser_unpaired")


@needs_db
def test_unpairing_in_the_extension_does_the_same(client: TestClient, me: dict[str, str]) -> None:
    e = paired(client, me)
    gone = e.unpair()
    assert (gone.status_code, gone.content) == (204, b"")
    assert refusal(e.poll()) == (401, "unpaired")
    assert refusal(e.unpair()) == (401, "unpaired")
    assert client.get(BASE, headers=me).json() == {"links": []}
    assert "browser_link" not in agent_config(client, me, "scout")


@needs_db
def test_deleting_the_agent_deletes_its_link(client: TestClient, me: dict[str, str], migrated_db_url: str) -> None:
    e, kept = paired(client, me), paired(client, me, "Kept")
    assert delete_agent(client, me, "scout").status_code == 204
    assert refusal(e.poll()) == (401, "unpaired")
    assert [link["agent"] for link in client.get(BASE, headers=me).json()["links"]] == ["kept"]
    assert kept.poll().json()["state"] == "paired"
    # Renamed, the agent stays paired: the link holds it by id, and says its handle now.
    item = agent_item(client, me, "kept")
    renamed = client.put(f"/api/v1/agents/{item['id']}", json={"name": "renamed"}, headers=me)
    assert renamed.status_code == 200, renamed.text
    assert kept.poll().json()["agent"] == {"name": "renamed", "label": "Kept"}
    (link,) = client.get(BASE, headers=me).json()["links"]
    assert (link["id"], link["agent_id"], link["agent"]) == (kept.id, item["id"], "renamed")
    assert agent_config(client, me, "renamed")["browser_link"] == kept.id
    # An agent gone without a word (its row deleted by hand): its extension is unpaired when
    # it next asks.
    with create_engine(migrated_db_url).begin() as conn:
        conn.execute(text("UPDATE browser_links SET agent_id = NULL WHERE id = :id"), {"id": kept.id})
    assert refusal(kept.poll()) == (401, "unpaired")
    assert client.get(BASE, headers=me).json() == {"links": []}


@needs_db
def test_only_the_claim_sets_an_agents_browser(client: TestClient, me: dict[str, str]) -> None:
    e = paired(client, me)
    item = agent_item(client, me, "scout")
    config = item["config"]
    # A save that leaves it out, or names another link, keeps the stored one…
    for sent in ({k: v for k, v in config.items() if k != "browser_link"}, {**config, "browser_link": "0" * 32}):
        saved = client.put(f"/api/v1/agents/{item['id']}", json={"config": {**sent, "instructions": "Be brief."}}, headers=me)
        assert saved.status_code == 200
        assert agent_config(client, me, "scout") == {**config, "instructions": "Be brief."}
    # …and a save cannot give another agent this browser, made or changed.
    _agent(client, me, "sky", {"label": "Sky", "browser_link": e.id})
    assert agent_config(client, me, "sky") == {"label": "Sky"}
    _agent(client, me, "sky", {"label": "Sky", "browser_link": e.id, "paired_kind": "wysebee-browser-chrome"})
    assert agent_config(client, me, "sky") == {"label": "Sky"}
    # A task's settings are not an agent's.
    task = client.post("/api/v1/tasks", json={"name": "t", "browser": e.id}, headers=me).json()
    assert task["browser"] == e.id


@needs_db
def test_deleting_the_account_deletes_its_links_and_calls(client: TestClient, me: dict[str, str], migrated_db_url: str, monkeypatch) -> None:
    from apps.cloud import account_deletion

    async def nothing(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(account_deletion, "can_delete_identities", lambda settings: True)
    monkeypatch.setattr(account_deletion, "delete_identity", nothing)
    e = paired(client, me)
    other = {"X-Test-User": f"test-{uuid.uuid4().hex}"}
    theirs = paired(client, other, "Theirs")
    try:
        client.post(f"{BASE}/{e.id}/calls", json={"op": "ping"}, headers=me)
        r = client.delete("/api/v1/account", headers=me)
        assert (r.status_code, r.json()) == (200, {"deleted": True})
        with create_engine(migrated_db_url).connect() as conn:
            for table in ("browser_links", "browser_calls"):
                assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE user_id = :u"), {"u": me["X-Test-User"]}).scalar() == 0, table
        assert refusal(e.poll()) == (401, "unpaired")
        assert theirs.poll().json()["state"] == "paired"
    finally:
        with create_engine(migrated_db_url).begin() as conn:
            for table in ("browser_calls", "browser_links", "user_agents", "user_tasks", "cloud_config_heads"):
                conn.execute(text(f"DELETE FROM {table} WHERE user_id = :u"), {"u": other["X-Test-User"]})


# --- the second kind: Wysebee Agent for Mac (docs/protocol/computer.md) ---------------

AGENT = "/api/v1/agent-links"
PROGRAM = {"name": "open_postman", "description": "Opens Postman", "command": "open", "args": [{"value": "/Applications/Postman.app"}, {"param": "file", "description": "A file"}]}


def mac(client: TestClient, me: dict[str, str], name: str = "Studio Mac", home: str = "desk-1") -> Extension:
    """Wysebee Agent for Mac, paired from a desktop: its link, with its agent made."""
    m = Extension(client, name, kind="osx", browser="Sam's MacBook", extension_version="0.1.0")
    r = client.post(f"{AGENT}/claim", json={"code": m.code, "home": home, "home_name": "Sam's Mac"}, headers=me)
    assert r.status_code == 200, r.text
    m.claimed = r.json()
    return m


def save(client: TestClient, me: dict[str, str], name: str, config: dict):
    """Save an agent's settings as a client does, with this config."""
    return client.put(f"/api/v1/agents/{agent_item(client, me, name)['id']}", json={"config": config}, headers=me)


def test_without_sign_in_the_persons_link_routes_are_refused(ui_dist: Path) -> None:
    with make_client(ui_dist, auth_issuer="https://tenant.example.auth0.com/") as c:
        for prefix in (BASE, AGENT):
            assert c.get(prefix).status_code == 401 and c.post(f"{prefix}/claim", json={}).status_code == 401
        assert c.get("/api/v1/agent-types").status_code == 401


@needs_db
def test_a_start_names_its_type(client: TestClient, me: dict[str, str], migrated_db_url: str) -> None:
    # 2026-11-13, later: a device names an enabled agent type's key; builds from before types
    # send "browser", "osx" or nothing.
    for prefix in (BASE, AGENT):
        for bad in ("ios", "", 5, ["osx"], "wysebee-browser-edge", "Wysebee-OSX"):
            assert refusal(client.post(f"{prefix}/start", json={"name": "Scout", "kind": bad, "secret_hash": "0" * 64})) == (422, "invalid_kind"), bad
    started = {kind: Extension(client, "Scout", **({"kind": kind} if kind != "none" else {})) for kind in ("wysebee-osx", "wysebee-browser-chrome", "osx", "browser", "none")}
    pc = Extension(client, "Studio PC", kind="wysebee-windows")
    with create_engine(migrated_db_url).connect() as conn:
        rows = {r.id: (r.kind, r.agent_type_id) for r in conn.execute(text("SELECT id, kind, agent_type_id::text FROM browser_links WHERE id = ANY(:i)"), {"i": [e.id for e in [*started.values(), pc]]})}
    mac, chrome = ("osx", OSX_ID), ("browser", CHROME_ID)
    # Wysebee Agent for Windows (2026-11-13, migration 0051): held where a Mac's link is, so
    # its legacy kind is osx, but a type of its own.
    assert [rows[e.id] for e in started.values()] == [mac, chrome, mac, chrome, chrome]
    assert rows[pc.id] == ("osx", WINDOWS_ID)
    r = client.post(f"{AGENT}/claim", json={"code": pc.code}, headers=me)
    assert r.status_code == 200, r.text
    assert (r.json()["link"]["agent_type"], r.json()["link"]["kind"]) == ({"id": WINDOWS_ID, "key": "wysebee-windows", "name": "Wysebee Windows Agent"}, "osx")
    windows = agent_config(client, me, r.json()["agent"]["name"])
    assert (windows["paired_kind"], windows["agent_type_id"]) == ("wysebee-windows", WINDOWS_ID)
    # A type Admin disabled pairs no more, under its key or the legacy name; what was started
    # still pairs, and says its type.
    engine = create_engine(migrated_db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE agent_types SET enabled = false WHERE key = 'wysebee-osx'"))
        agent_types.forget()
        for kind in ("wysebee-osx", "osx"):
            assert refusal(client.post(f"{BASE}/start", json={"name": "Scout", "kind": kind, "secret_hash": "0" * 64})) == (422, "invalid_kind")
        r = client.post(f"{AGENT}/claim", json={"code": started["wysebee-osx"].code}, headers=me)
        assert r.status_code == 200, r.text
        assert (r.json()["link"]["agent_type"], r.json()["link"]["kind"]) == ({"id": OSX_ID, "key": "wysebee-osx", "name": "Wysebee OSX Agent"}, "osx")
    finally:
        with engine.begin() as conn:
            conn.execute(text("UPDATE agent_types SET enabled = true WHERE key = 'wysebee-osx'"))
        engine.dispose()


@needs_db
def test_a_linux_machine_pairs_as_its_own_type_and_takes_no_screen_op(client: TestClient, me: dict[str, str], migrated_db_url: str) -> None:
    # Wysebee Agent for Linux (2026-11-18, migration 0079): held where a Mac's link is, a type
    # of its own — the Windows type's ops without the screen's.
    vm = Extension(client, "Build VM", kind="wysebee-linux")
    with create_engine(migrated_db_url).connect() as conn:
        row = conn.execute(text("SELECT kind, agent_type_id::text FROM browser_links WHERE id = :i"), {"i": vm.id}).first()
    assert tuple(row) == ("osx", LINUX_ID)
    r = client.post(f"{AGENT}/claim", json={"code": vm.code}, headers=me)
    assert r.status_code == 200, r.text
    assert (r.json()["link"]["agent_type"], r.json()["link"]["kind"]) == ({"id": LINUX_ID, "key": "wysebee-linux", "name": "Wysebee Linux Agent"}, "osx")
    linux = agent_config(client, me, r.json()["agent"]["name"])
    assert (linux["paired_kind"], linux["agent_type_id"]) == ("wysebee-linux", LINUX_ID)
    vm.poll()
    for op in ("screen_screenshot", "screen_action", "ios_dev"):
        assert refusal(client.post(f"{AGENT}/{vm.id}/calls", json={"op": op}, headers=me)) == (422, "invalid_op"), op
    for op in ("program_run", "dev_shell", "android_dev"):
        assert client.post(f"{AGENT}/{vm.id}/calls", json={"op": op}, headers=me).status_code == 200, op


@needs_db
def test_a_links_type_says_what_it_may_do_and_a_broken_one_does_nothing(client: TestClient, me: dict[str, str], migrated_db_url: str) -> None:
    m = mac(client, me)
    m.poll()
    engine = create_engine(migrated_db_url)
    with engine.begin() as conn:
        ops = conn.execute(text("SELECT ops::text FROM agent_types WHERE key = 'wysebee-osx'")).scalar_one()
    try:
        # An op Admin adds to the type's row is relayed; one it does not list is refused.
        assert refusal(client.post(f"{AGENT}/{m.id}/calls", json={"op": "screen_capture"}, headers=me)) == (422, "invalid_op")
        with engine.begin() as conn:
            conn.execute(text("UPDATE agent_types SET ops = ops || '[\"screen_capture\"]'::jsonb WHERE key = 'wysebee-osx'"))
        agent_types.forget()
        sent = client.post(f"{AGENT}/{m.id}/calls", json={"op": "screen_capture"}, headers=me).json()
        assert [c["op"] for c in m.poll().json()["calls"]] == ["screen_capture"]
        client.post(f"{AGENT}/calls/{sent['id']}/cancel", headers=me)
        # A row that breaks the table's limits is no type: its links take no op, a code of it
        # pairs nothing (the code stays), and the list names no type for them.
        waiting = Extension(client, "Other Mac", kind="wysebee-osx")
        with engine.begin() as conn:
            conn.execute(text("UPDATE agent_types SET ops = '\"ping\"'::jsonb WHERE key = 'wysebee-osx'"))
        agent_types.forget()
        assert refusal(client.post(f"{AGENT}/{m.id}/calls", json={"op": "ping"}, headers=me)) == (422, "invalid_op")
        assert refusal(client.post(f"{AGENT}/claim", json={"code": waiting.code}, headers=me)) == (422, "invalid_kind")
        assert waiting.poll().json()["state"] == "pending"
        (link,) = client.get(AGENT, headers=me).json()["links"]
        assert (link["agent_type"], link["kind"]) == (None, "osx")
        assert {t["key"] for t in client.get("/api/v1/agent-types", headers=me).json()["types"]} == {"wysebee-android", "wysebee-browser-chrome", "wysebee-linux", "wysebee-windows"}
    finally:
        with engine.begin() as conn:
            conn.execute(text("UPDATE agent_types SET ops = CAST(:ops AS jsonb) WHERE key = 'wysebee-osx'"), {"ops": ops})
        engine.dispose()


@needs_db
def test_both_prefixes_serve_both_kinds(client: TestClient, me: dict[str, str], migrated_db_url: str) -> None:
    # Started at one prefix, a link is polled, claimed, listed, driven and unpaired at the other.
    b = Extension(client, "Scout")
    m = Extension(client, "Studio Mac", kind="osx")
    started = client.post(f"{AGENT}/start", json={"name": "Third", "secret_hash": "0" * 64})
    assert started.status_code == 200 and set(started.json()) == {"id", "code", "expires_in"}
    with create_engine(migrated_db_url).connect() as conn:
        kinds = dict(conn.execute(text("SELECT id, kind FROM browser_links WHERE id = ANY(:i)"), {"i": [b.id, m.id, started.json()["id"]]}).all())
    assert kinds == {b.id: "browser", m.id: "osx", started.json()["id"]: "browser"}
    assert client.post(f"{AGENT}/{b.id}/poll", json={}, headers=ext(b.secret)).json()["state"] == "pending"
    assert m.poll().json()["state"] == "pending"
    assert client.post(f"{AGENT}/claim", json={"code": b.code}, headers=me).status_code == 200
    assert client.post(f"{BASE}/claim", json={"code": m.code, "home": "desk-1"}, headers=me).status_code == 200
    assert b.poll().json()["state"] == m.poll().json()["state"] == "paired"
    for prefix in (BASE, AGENT, f"{AGENT}/"):
        links = client.get(prefix, headers=me).json()["links"]
        assert [(link["id"], link["kind"], link["approved"]) for link in links] == [(b.id, "browser", []), (m.id, "osx", [])], prefix
    sent = client.post(f"{AGENT}/{b.id}/calls", json={"op": "ping"}, headers=me).json()
    assert client.get(f"{AGENT}/calls/{sent['id']}", headers=me).json() == client.get(f"{BASE}/calls/{sent['id']}", headers=me).json() == sent
    assert client.post(f"{AGENT}/calls/{sent['id']}/cancel", headers=me).status_code == 204
    assert client.post(f"{AGENT}/{b.id}/unpair", headers=me).status_code == 204
    assert client.delete(f"{AGENT}/{m.id}", headers=ext(m.secret)).status_code == 204
    assert client.get(AGENT, headers=me).json() == {"links": []}


@needs_db
def test_typing_a_macs_code_makes_an_agent_for_that_mac(client: TestClient, me: dict[str, str]) -> None:
    # Typed on the website (2026-11-13, the owner's call: a Mac may have no Wysebee Desktop): the
    # agent lives in Wysebee Cloud, as a browser's does — with "home" said or left out.
    for i, body in enumerate(({"home": "cloud"}, {})):
        web = Extension(client, f"Web Mac {i}", kind="osx", browser="Air")
        r = client.post(f"{BASE}/claim", json={"code": web.code, **body}, headers=me)
        assert r.status_code == 200, r.text
        assert (r.json()["link"]["kind"], r.json()["agent"]["name"]) == ("osx", f"web-mac-{i}")
        assert agent_config(client, me, f"web-mac-{i}") == {"label": f"Web Mac {i}", "mac_link": web.id, "mac_folders": [], "paired_kind": "wysebee-osx", "agent_type_id": OSX_ID}
        assert web.poll().json()["state"] == "paired"
    # Typed in Wysebee Desktop, which names itself as the home: it lives in the cloud all the
    # same (2026-11-13: no agent lives on Wysebee Desktop).
    m = Extension(client, "Studio Mac", kind="osx", browser="Sam's MacBook", extension_version="0.1.0")
    r = client.post(f"{AGENT}/claim", json={"code": m.code, "home": "desk-1", "home_name": "Sam's Mac"}, headers=me)
    assert r.status_code == 200, r.text
    assert r.json()["agent"] == {"name": "studio-mac", "label": "Studio Mac"}
    link = r.json()["link"]
    assert {k: link[k] for k in ("id", "agent", "label", "browser", "extension_version", "status", "kind", "approved")} == {
        "id": m.id, "agent": "studio-mac", "label": "Studio Mac", "browser": "Sam's MacBook", "extension_version": "0.1.0", "status": "offline", "kind": "osx", "approved": []}
    # No browser fields: the agent names its Mac.
    assert agent_config(client, me, "studio-mac") == {"label": "Studio Mac", "mac_link": m.id, "mac_folders": [], "paired_kind": "wysebee-osx", "agent_type_id": OSX_ID}
    assert m.poll().json() == {"state": "paired", "agent": {"name": "studio-mac", "label": "Studio Mac"}, "calls": [], "cancelled": [], "programs": [], "skills": []}
    # The refusals a browser's claim has are a Mac's too, and are said to the app.
    taken = Extension(client, "Studio Mac", kind="osx")
    r = client.post(f"{AGENT}/claim", json={"code": taken.code, "home": "desk-1"}, headers=me)
    assert refusal(r) == (409, "name_taken")
    # Still paired with its Mac: not to be reconnected to another.
    assert r.json()["reconnect"] is False
    assert taken.poll().json()["state"] == "pending"


@needs_db
def test_only_the_claim_sets_an_agents_mac(client: TestClient, me: dict[str, str]) -> None:
    m, b = mac(client, me), paired(client, me)
    config = agent_config(client, me, "studio-mac")
    # Left out, another link's, or a browser's field on a Mac's agent: the links table says.
    for sent in ({k: v for k, v in config.items() if k != "mac_link"}, {**config, "mac_link": "0" * 32}, {**config, "mac_link": b.id}, {**config, "browser_link": m.id}, {**config, "browser_link": b.id}):
        assert save(client, me, "studio-mac", {**sent, "instructions": "Be brief."}).status_code == 200
        assert agent_config(client, me, "studio-mac") == {**config, "instructions": "Be brief."}
    # A browser's agent cannot be given the Mac, nor a plain one either.
    scout = agent_config(client, me, "scout")
    assert save(client, me, "scout", {**scout, "mac_link": m.id}).status_code == 200
    assert agent_config(client, me, "scout") == scout
    _agent(client, me, "sky", {"label": "Sky", "mac_link": m.id, "browser_link": b.id})
    assert agent_config(client, me, "sky") == {"label": "Sky"}


@needs_db
def test_a_mac_says_what_it_allowed_and_is_told_the_agents_programs(client: TestClient, me: dict[str, str]) -> None:
    m = mac(client, me)
    config = agent_config(client, me, "studio-mac")
    assert m.poll(approved=["open_postman"], browser="Studio", extension_version="0.2.0").json()["programs"] == []
    (link,) = client.get(AGENT, headers=me).json()["links"]
    assert (link["approved"], link["browser"], link["extension_version"], link["status"]) == (["open_postman"], "Studio", "0.2.0", "connected")
    # What is not a list of at most 20 names of at most 40 characters is ignored.
    for bad in ("open_postman", [5], ["x" * 41], [""], [" padded "], ["two\nlines"], [f"p{i}" for i in range(21)], {"a": 1}, None):
        m.poll(approved=bad)
        assert client.get(AGENT, headers=me).json()["links"][0]["approved"] == ["open_postman"], bad
    m.poll(approved=[f"p{i}" for i in range(20)])
    assert len(client.get(AGENT, headers=me).json()["links"][0]["approved"]) == 20
    m.poll(approved=[])
    assert client.get(AGENT, headers=me).json()["links"][0]["approved"] == []
    # The agent's programs: its program tools, in order; a save of its settings leaves them be.
    ids = [make_tool(client, me, "program", {**GIVEN["program"][1], "name": f"p{i}"}) for i in range(20)]
    assert give(client, me, "studio-mac", "program", ids[:1]).status_code == 200
    assert m.poll().json()["programs"] == [{**PROGRAM, "name": "p0"}]
    assert save(client, me, "studio-mac", {**config, "local_programs": [PROGRAM]}).status_code == 200
    assert "local_programs" not in agent_config(client, me, "studio-mac")
    assert give(client, me, "studio-mac", "program", ids[::-1]).status_code == 200
    assert [p["name"] for p in m.poll().json()["programs"]] == [f"p{i}" for i in range(19, -1, -1)]
    # A browser says nothing of programs, and is told none.
    b = paired(client, me)
    assert "programs" not in b.poll(approved=["open_postman"]).json()
    assert next(link for link in client.get(BASE, headers=me).json()["links"] if link["id"] == b.id)["approved"] == []


@needs_db
def test_a_macs_waiting_poll_answers_when_its_programs_change(client: TestClient, me: dict[str, str]) -> None:
    m = mac(client, me)
    config = agent_config(client, me, "studio-mac")
    assert m.poll().json()["programs"] == []
    answers: list = []
    waiting = threading.Thread(target=lambda: answers.append(m.poll(wait=20).json()))
    started = time.monotonic()
    waiting.start()
    time.sleep(0.5)
    # A save that changes no program does not answer it; one that does, at once.
    assert save(client, me, "studio-mac", {**config, "instructions": "Be brief."}).status_code == 200
    time.sleep(1.5)
    assert not answers
    assert give(client, me, "studio-mac", "program", [make_tool(client, me, "program", GIVEN["program"][1])]).status_code == 200
    waiting.join(timeout=10)
    assert answers and answers[0]["programs"] == [PROGRAM] and answers[0]["calls"] == [] and time.monotonic() - started < 8
    # A program changed, or deleted: at once too.
    tool = client.get("/api/v1/tools?kind=program", headers=me).json()["tools"][0]
    for change in (lambda: client.put(f"/api/v1/tools/{tool['id']}", json={**GIVEN["program"][1], "description": "Opens it"}, headers=me), lambda: client.delete(f"/api/v1/tools/{tool['id']}", headers=me)):
        answers.clear()
        waiting = threading.Thread(target=lambda: answers.append(m.poll(wait=20).json()))
        started = time.monotonic()
        waiting.start()
        time.sleep(0.5)
        assert change().status_code in (200, 204)
        waiting.join(timeout=10)
        assert answers and time.monotonic() - started < 8


@needs_db
def test_a_mac_runs_programs_and_a_browser_does_not(client: TestClient, me: dict[str, str]) -> None:
    m, b = mac(client, me), paired(client, me)
    assert m.poll().json()["state"] == "paired"
    # Each kind takes its own ops; `ping` is both's.
    for op in ("browser_tabs", "browser_click", "recording_saved"):
        r = client.post(f"{AGENT}/{m.id}/calls", json={"op": op}, headers=me)
        assert refusal(r) == (422, "invalid_op") and "Mac" in r.json()["detail"], op
    r = client.post(f"{BASE}/{b.id}/calls", json={"op": "local_run", "args": {"name": "open_postman"}}, headers=me)
    assert refusal(r) == (422, "invalid_op") and "browser" in r.json()["detail"]
    assert refusal(client.post(f"{AGENT}/{m.id}/calls", json={"op": "shell"}, headers=me)) == (422, "invalid_op")
    assert refusal(client.post(f"{AGENT}/{'0' * 32}/calls", json={"op": "local_run"}, headers=me)) == (404, "browser_unpaired")
    assert client.post(f"{BASE}/{b.id}/calls", json={"op": "ping"}, headers=me).json()["status"] == "waiting"

    sent = client.post(f"{AGENT}/{m.id}/calls", json={"op": "local_run", "args": {"name": "open_postman", "values": {"file": "a.json"}}, "timeout_seconds": 75}, headers=me).json()
    assert sent["status"] == "waiting"
    (call,) = m.poll().json()["calls"]
    assert call == {"id": sent["id"], "op": "local_run", "args": {"name": "open_postman", "values": {"file": "a.json"}}}
    out = {"exit_code": 0, "stdout": "ok\n", "stderr": ""}
    assert m.result(call["id"], ok=True, result=out).status_code == 204
    assert client.get(f"{AGENT}/calls/{sent['id']}", headers=me).json() == {"id": sent["id"], "status": "done", "ok": True, "result": out}
    # A refusal with no words is said of the Mac.
    sent = client.post(f"{AGENT}/{m.id}/calls", json={"op": "ping"}, headers=me).json()
    (call,) = m.poll().json()["calls"]
    assert m.result(call["id"], ok=False).status_code == 204
    assert "Wysebee Agent for Mac" in client.get(f"{AGENT}/calls/{sent['id']}", headers=me).json()["error"]
    # The cloud's own loop is refused the same way, and is not told the Mac was unavailable.
    state = client.app.state
    mine = agent_item(client, me, "studio-mac")["id"]
    got = client.portal.call(lambda: browser_links.run_op(state, me["X-Test-User"], m.id, "browser_tabs", {}, agent=mine))
    assert got["ok"] is False and got["unavailable"] is False and "Mac" in got["error"]


@needs_db
def test_a_mac_that_is_not_there_is_said_of_the_mac(ui_dist: Path, migrated_db_url: str, tmp_path: Path, me: dict[str, str]) -> None:
    c = _client(ui_dist, migrated_db_url, tmp_path, browser_link_starts_per_minute=10_000, browser_link_online_seconds=0.5, **FAST)
    try:
        m = mac(c, me)
        # Never polled since it was paired, or not lately: offline, said at once.
        r = c.post(f"{AGENT}/{m.id}/calls", json={"op": "local_run"}, headers=me)
        assert refusal(r) == (409, "browser_offline") and r.json()["detail"] == ComputerPack.words[browser_links.OFFLINE]
        assert "Mac is asleep or offline" in r.json()["detail"]
        # Connected, but it does not take the call in time.
        m.poll()
        failed = c.post(f"{AGENT}/{m.id}/calls?wait=5", json={"op": "local_run"}, headers=me).json()
        assert (failed["status"], failed["error"]) == ("failed", ComputerPack.words[browser_links.NOT_REACHED])
        # …which the cloud's own loop would call unavailable.
        m.poll()
        mine = agent_item(c, me, "studio-mac")["id"]
        got = c.portal.call(lambda: browser_links.run_op(c.app.state, me["X-Test-User"], m.id, "ping", {}, agent=mine))
        assert got == {"ok": False, "error": ComputerPack.words[browser_links.NOT_REACHED], "unavailable": True}
        # Taken, and not answered within its own time: stopped, in the Mac's words.
        m.poll()
        sent = c.post(f"{AGENT}/{m.id}/calls", json={"op": "local_run", "timeout_seconds": 0.3}, headers=me).json()
        assert len(m.poll().json()["calls"]) == 1
        time.sleep(0.4)
        late = c.get(f"{AGENT}/calls/{sent['id']}", headers=me).json()
        assert late["status"] == "failed" and late["error"].startswith("The Mac did not finish this within 0 seconds")
        assert m.poll().json()["cancelled"] == [sent["id"]]
        # Unpaired: its next poll says so of the Mac; a wrong secret learns nothing of the kind.
        assert c.post(f"{AGENT}/{m.id}/unpair", headers=me).status_code == 204
        again = mac(c, me, "Other Mac")
        assert c.post(f"{AGENT}/{again.id}/poll", json={}, headers=ext("wrong")).json()["detail"] == browser_links.UNPAIRED
        assert c.post(f"{AGENT}/{again.id}/poll", json={}, headers={"Authorization": f"Basic {again.secret}"}).json()["detail"] == ComputerPack.words[browser_links.UNPAIRED]
        assert delete_agent(c, me, "other-mac").status_code == 204
        assert refusal(again.poll()) == (401, "unpaired")
    finally:
        c.__exit__(None, None, None)


@needs_db
def test_unpairing_a_mac_leaves_its_agent_without_it(client: TestClient, me: dict[str, str]) -> None:
    m = mac(client, me)
    assert give(client, me, "studio-mac", "program", [make_tool(client, me, "program", GIVEN["program"][1])]).status_code == 200
    before = _revision(client, me)
    assert client.post(f"{AGENT}/{m.id}/unpair", headers=me).status_code == 204
    assert refusal(m.poll()) == (401, "unpaired")
    # The agent stays, with its programs (to remove) and no Mac: a new revision.
    assert agent_config(client, me, "studio-mac") == {"label": "Studio Mac", "paired_kind": "wysebee-osx", "agent_type_id": OSX_ID}
    assert [t["name"] for t in client.get(f"/api/v1/agents/{agent_item(client, me, 'studio-mac')['id']}/tools", headers=me).json()["tools"]] == [PROGRAM["name"]]
    assert _revision(client, me) == before + 1
    # From the app's side the same; and an agent whose Mac is its link is unpaired when it goes.
    other = mac(client, me, "Other Mac")
    assert other.unpair().status_code == 204
    assert "mac_link" not in agent_config(client, me, "other-mac")
    third = mac(client, me, "Third Mac")
    assert delete_agent(client, me, "third-mac").status_code == 204
    assert refusal(third.poll()) == (401, "unpaired")
    assert client.get(AGENT, headers=me).json() == {"links": []}


# --- which agent may add which tool ---------------------------------------------------------------

SITES = [{"description": "Work", "address": "https://example.com/"}]
# The tool a save of the settings can add (the rest are tools of their own since migration
# 0061): its kind, and the settings that have it.
TOOLS = {"devtools": ("browser", {"browser_devtools": True})}
# Each tool given (apps/cloud/user_tools.py): the kind of agent that may have it, and the tool.
SITE = {"description": "Work", "config": {"address": "https://example.com/"}}
GIVEN = {
    "program": ("osx", {"name": PROGRAM["name"], "description": PROGRAM["description"], "config": {"command": PROGRAM["command"], "args": PROGRAM["args"]}}),
    "gmail": ("browser", {"description": "Work", "config": {"address": "https://mail.google.com/mail/u/0/"}}),
    **{k: ("browser", SITE) for k in ("calendar", "travel", "news", "website")},
}


def make_tool(client: TestClient, me: dict[str, str], kind: str, body: dict) -> str:
    r = client.post("/api/v1/tools", json={"kind": kind, **body}, headers=me)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def give(client: TestClient, me: dict[str, str], agent: str, kind: str, ids: list[str]):
    return client.put(f"/api/v1/agents/{agent_item(client, me, agent)['id']}/tools", json={"kind": kind, "tool_ids": ids}, headers=me)


def wrong_kind(r) -> str:
    assert (r.status_code, r.json()["code"]) == (422, "wrong_agent_kind"), r.text
    assert set(r.json()) == {"detail", "code"} and "Devices" in r.json()["detail"]
    return r.json()["detail"]


@needs_db
def test_a_tool_an_agent_already_had_is_kept_changed_or_removed(client: TestClient, me: dict[str, str], migrated_db_url: str) -> None:
    # An agent from before the rule: a plain one with the web development tools, and site links
    # and programs still in its settings (from before migration 0061).
    old = {"label": "Old", "browser_devtools": True, "websites": SITES, "local_programs": [PROGRAM]}
    _agent(client, me, "old", {"label": "Old"})
    with create_engine(migrated_db_url).begin() as conn:
        conn.execute(text("UPDATE user_agents SET config = CAST(:c AS jsonb) WHERE user_id = :u AND name = 'old'"), {"c": json.dumps(old), "u": me["X-Test-User"]})
    assert agent_config(client, me, "old") == old
    # Kept, and changed while it stays; the links and programs go from the settings, which are
    # tools now.
    assert save(client, me, "old", {**old, "instructions": "Be brief."}).status_code == 200
    assert agent_config(client, me, "old") == {"label": "Old", "browser_devtools": True, "instructions": "Be brief."}
    # Removed; once gone, it cannot come back.
    assert save(client, me, "old", {"label": "Old"}).status_code == 200
    wrong_kind(save(client, me, "old", {"label": "Old", "browser_devtools": True}))
    # A site link it already has (given before the rule): kept, and others of that kind given
    # with it; taken away, none comes back.
    site = make_tool(client, me, "website", SITE)
    other = make_tool(client, me, "website", {**SITE, "config": {"address": "https://docs.example.com/"}})
    agent = agent_item(client, me, "old")["id"]
    with create_engine(migrated_db_url).begin() as conn:
        conn.execute(text("INSERT INTO agent_tools (user_id, agent_id, tool_id, position) VALUES (:u, CAST(:a AS uuid), CAST(:t AS uuid), 0)"), {"u": me["X-Test-User"], "a": agent, "t": site})
    assert give(client, me, "old", "website", [site, other]).status_code == 200
    assert give(client, me, "old", "website", []).status_code == 200
    wrong_kind(give(client, me, "old", "website", [site]))
    # Unpaired, a browser's agent keeps its tools but is plain: it gets no new one.
    b = paired(client, me)
    config = agent_config(client, me, "scout")
    assert save(client, me, "scout", {**config, "browser_devtools": True}).status_code == 200
    news = make_tool(client, me, "news", SITE)
    assert give(client, me, "scout", "news", [news]).status_code == 200
    assert client.post(f"{BASE}/{b.id}/unpair", headers=me).status_code == 204
    left = agent_config(client, me, "scout")
    assert left["browser_devtools"] is True and "browser_link" not in left
    assert save(client, me, "scout", {**left, "instructions": "Still here."}).status_code == 200
    assert give(client, me, "scout", "news", [news]).status_code == 200
    wrong_kind(give(client, me, "scout", "gmail", [make_tool(client, me, "gmail", GIVEN["gmail"][1])]))


@needs_db
def test_a_tool_is_added_only_to_the_kind_of_agent_that_can_have_it(client: TestClient, me: dict[str, str]) -> None:
    mac(client, me)
    paired(client, me)
    _agent(client, me, "plain", {"label": "Plain"})
    agents = {"plain": None, "scout": "browser", "studio-mac": "osx"}
    made = {kind: make_tool(client, me, kind, body) for kind, (_, body) in GIVEN.items()}
    for name, kind in agents.items():
        config = agent_config(client, me, name)
        for tool, (needs, has) in TOOLS.items():
            r = save(client, me, name, {**config, **has})
            if needs == kind:
                assert r.status_code == 200, (name, tool, r.text)
                assert save(client, me, name, config).status_code == 200
            else:
                assert "browser extension" in wrong_kind(r)
                assert agent_config(client, me, name) == config
        for tool, (needs, _) in GIVEN.items():
            r = give(client, me, name, tool, [made[tool]])
            if needs == kind:
                assert r.status_code == 200, (name, tool, r.text)
            else:
                said = wrong_kind(r)
                assert ("Wysebee Agent for Mac" in said) == (needs == "osx") and ("browser extension" in said) == (needs == "browser"), (name, tool)
            # Taking it away, or giving none, is always allowed.
            assert give(client, me, name, tool, []).status_code == 200
        # What is not the tool leaves the agent's kind out of it: a switch off.
        assert save(client, me, name, {**config, "browser_devtools": False}).status_code == 200
        assert save(client, me, name, config).status_code == 200


@needs_db
def test_a_new_agent_cannot_be_made_with_another_kinds_tool(client: TestClient, me: dict[str, str]) -> None:
    for tool, (_, has) in TOOLS.items():
        wrong_kind(client.post("/api/v1/agents", json={"name": "new", "config": {"label": "New", **has}}, headers=me))
    assert agents_of(client, me) == []
    # A deleted agent's tools are not the next one's of that name.
    _agent(client, me, "new", {"label": "New"})
    assert delete_agent(client, me, "new").status_code == 204
    wrong_kind(client.post("/api/v1/agents", json={"name": "new", "config": {"label": "New", "browser_devtools": True}}, headers=me))
    # A task's settings are not an agent's.
    assert client.post("/api/v1/tasks", json={"name": "t", "browser_devtools": True}, headers=me).status_code == 201


# --- skill scripts: the Mac's skills, their files, and skill_run (the Mac protocol's "Skill scripts")


MANIFEST = b"---\nname: forecast\ndescription: Weather.\nscripts:\n  - name: run\n    command: python3 run.py\n---\nRun it.\n"


def put_file(client: TestClient, who: dict[str, str], data: bytes) -> dict:
    r = client.put("/api/v1/skill-files", content=data, headers=who)
    assert r.status_code == 200, r.text
    return r.json()


def put_skill(client: TestClient, who: dict[str, str], name: str, files: list[dict], *, rename: str | None = None) -> dict:
    """Make the skill named ``name`` with exactly ``files`` (``[{path, id}]`` uploaded to
    /skill-files, SKILL.md among them), or change it so (and rename it to ``rename``). Answers
    the skill — its SKILL.md as kept, its ``name`` the skill's."""
    found = next((s for s in client.get("/api/v1/skills", headers=who).json()["skills"] if s["name"] == name), None)
    if found is None:
        found = client.post("/api/v1/skills", json={"name": name}, headers=who).json()
    if rename:
        found = client.put(f"/api/v1/skills/{found['id']}", json={"name": rename}, headers=who).json()
    for f in files:
        r = _put_skill_file(client, who, found["id"], f["path"], _bytes(client, who, f))
        assert r.status_code == 200, r.text
    for f in found["files"]:
        if f["path"] not in {g["path"] for g in files}:
            assert client.delete(f"/api/v1/skills/{found['id']}/files/{f['path']}", headers=who).status_code == 204
    return client.get(f"/api/v1/skills/{found['id']}", headers=who).json()


def kept(skill: dict) -> list[dict]:
    """A skill's files as the Mac is told them."""
    return [{"path": f["path"], "id": f["id"], "size": f["size"]} for f in skill["files"]]


def skill_file_url(link: Extension, file_id: str, prefix: str = AGENT) -> str:
    return f"{prefix}/{link.id}/skill-files/{file_id}"


@needs_db
def test_a_mac_is_told_its_agents_skills_and_says_which_it_allowed(client: TestClient, me: dict[str, str]) -> None:
    m = mac(client, me)
    assert m.poll().json()["skills"] == []
    manifest, notes = put_file(client, me, MANIFEST), put_file(client, me, b"Sunny.")
    files = [{"path": "SKILL.md", **manifest}, {"path": "notes/today.md", **notes}]
    forecast = put_skill(client, me, "forecast", files)
    bare = put_skill(client, me, "bare", files[:1])
    put_skill(client, me, "unused", files)
    # Its SKILL.md is kept as it was sent: its name is the skill's already.
    assert kept(forecast) == files
    # In the agent's order.
    _give(client, me, "studio-mac", ["bare", "forecast"])
    assert m.poll().json()["skills"] == [{"name": "bare", "files": kept(bare)}, {"name": "forecast", "files": files}]
    # Renamed: told by its new name, still the agent's.
    put_skill(client, me, "bare", files[:1], rename="plain")
    assert [s["name"] for s in m.poll().json()["skills"]] == ["plain", "forecast"]
    # A deleted skill goes from the list.
    plain = next(s for s in client.get("/api/v1/skills", headers=me).json()["skills"] if s["name"] == "plain")
    assert client.delete(f"/api/v1/skills/{plain['id']}", headers=me).status_code == 204
    assert m.poll().json()["skills"] == [{"name": "forecast", "files": files}]
    # What the Mac allowed: at most 50 names of at most 40 characters, else ignored.
    m.poll(approved_skills=["forecast"])
    (link,) = client.get(AGENT, headers=me).json()["links"]
    assert (link["approved_skills"], link["approved"]) == (["forecast"], [])
    for bad in ("forecast", [5], ["x" * 41], [""], [" padded "], ["two\nlines"], [f"s{i}" for i in range(51)], {"a": 1}, None):
        m.poll(approved_skills=bad)
        assert client.get(AGENT, headers=me).json()["links"][0]["approved_skills"] == ["forecast"], bad
    m.poll(approved_skills=[f"s{i}" for i in range(50)])
    assert len(client.get(AGENT, headers=me).json()["links"][0]["approved_skills"]) == 50
    m.poll(approved_skills=[])
    assert client.get(AGENT, headers=me).json()["links"][0]["approved_skills"] == []
    # A browser is told no skills, and what it says of them is not kept.
    b = paired(client, me)
    _give(client, me, "scout", ["forecast"])
    assert "skills" not in b.poll(approved_skills=["forecast"]).json()
    assert next(link for link in client.get(BASE, headers=me).json()["links"] if link["id"] == b.id)["approved_skills"] == []


@needs_db
def test_a_macs_waiting_poll_answers_when_one_of_its_skills_changes(client: TestClient, me: dict[str, str]) -> None:
    m = mac(client, me)
    manifest, notes = put_file(client, me, MANIFEST), put_file(client, me, b"Sunny.")
    first = [{"path": "SKILL.md", **manifest}]
    put_skill(client, me, "forecast", first)
    unrelated = [{"path": "SKILL.md", "data": b"---\nname: unrelated\ndescription: Not the Mac's.\n---\nX.\n"}]
    _give(client, me, "studio-mac", ["forecast"])
    assert m.poll().json()["skills"] == [{"name": "forecast", "files": first}]

    def waiting() -> tuple[list, threading.Thread]:
        answers: list = []
        thread = threading.Thread(target=lambda: answers.append(m.poll(wait=20).json()))
        thread.start()
        time.sleep(0.5)
        return answers, thread

    # A skill its agent does not have: not answered.
    answers, thread = waiting()
    started = time.monotonic()
    put_skill(client, me, "unrelated", unrelated)
    time.sleep(1.5)
    assert not answers
    # Its own skill changed: at once.
    files = [*first, {"path": "notes.md", **notes}]
    forecast = put_skill(client, me, "forecast", files)
    thread.join(timeout=10)
    assert answers and answers[0]["skills"] == [{"name": "forecast", "files": files}] and time.monotonic() - started < 8
    # …taken off the agent…
    answers, thread = waiting()
    started = time.monotonic()
    _give(client, me, "studio-mac", [])
    thread.join(timeout=10)
    assert answers and answers[0]["skills"] == [] and time.monotonic() - started < 8
    # …and, given again, deleted.
    _give(client, me, "studio-mac", ["forecast"])
    assert m.poll().json()["skills"] == [{"name": "forecast", "files": files}]
    answers, thread = waiting()
    started = time.monotonic()
    assert client.delete(f"/api/v1/skills/{forecast['id']}", headers=me).status_code == 204
    thread.join(timeout=10)
    assert answers and answers[0]["skills"] == [] and time.monotonic() - started < 8


@needs_db
def test_a_mac_downloads_only_its_agents_skill_files(client: TestClient, me: dict[str, str], migrated_db_url: str) -> None:
    m = mac(client, me)
    manifest, notes, loose, gone = put_file(client, me, MANIFEST), put_file(client, me, b"Sunny."), put_file(client, me, b"Not attached."), put_file(client, me, b"Soon gone.")
    other = {"X-Test-User": f"test-{uuid.uuid4().hex}"}
    try:
        theirs = put_file(client, other, b"Someone else's.")
        put_skill(client, me, "forecast", [{"path": "SKILL.md", **manifest}, {"path": "notes.md", **notes}, {"path": "gone.md", **gone}])
        put_skill(client, me, "unused", [{"path": "SKILL.md", "data": b"---\nname: unused\ndescription: U.\n---\nU.\n"}, {"path": "x.md", **loose}])
        # Listed by the skill, but no longer stored.
        assert client.delete(f"/api/v1/skill-files/{gone['id']}", headers=me).status_code == 204
        _give(client, me, "studio-mac", ["forecast"])
        for prefix in (AGENT, BASE):
            r = client.get(skill_file_url(m, manifest["id"], prefix), headers=ext(m.secret))
            assert r.status_code == 200 and r.content == MANIFEST and r.headers["content-type"] == "application/octet-stream", prefix
            assert r.headers["x-content-type-options"] == "nosniff"
        assert client.get(skill_file_url(m, notes["id"]), headers=ext(m.secret)).content == b"Sunny."
        # Not attached, another account's, not stored, not an id: all the same 404.
        for file_id in (loose["id"], theirs["id"], gone["id"], "f" * 64, "F" * 64, "xyz", "a" * 65):
            assert refusal(client.get(skill_file_url(m, file_id), headers=ext(m.secret))) == (404, "file_unknown"), file_id
        # A wrong secret, or none, is the poll's 401.
        assert refusal(client.get(skill_file_url(m, manifest["id"]), headers=ext("wrong"))) == (401, "unpaired")
        assert refusal(client.get(skill_file_url(m, manifest["id"]))) == (401, "unpaired")
        # A browser's link, even with the skill on its agent; a Mac not paired yet.
        b = paired(client, me)
        _give(client, me, "scout", ["forecast"])
        assert refusal(client.get(skill_file_url(b, manifest["id"], BASE), headers=ext(b.secret))) == (404, "file_unknown")
        waiting = Extension(client, "Waiting Mac", kind="osx")
        assert refusal(client.get(skill_file_url(waiting, manifest["id"]), headers=ext(waiting.secret))) == (404, "file_unknown")
        # The skill taken off the agent: its files with it.
        _give(client, me, "studio-mac", [])
        assert refusal(client.get(skill_file_url(m, manifest["id"]), headers=ext(m.secret))) == (404, "file_unknown")
    finally:
        with create_engine(migrated_db_url).begin() as conn:
            conn.execute(text("DELETE FROM cloud_skill_files WHERE user_id = :u"), {"u": other["X-Test-User"]})


@needs_db
def test_a_mac_runs_skill_scripts_and_a_browser_does_not(client: TestClient, me: dict[str, str]) -> None:
    m, b = mac(client, me), paired(client, me)
    m.poll()
    args = {"skill": "forecast", "script": "run", "args": ["--city", "Paris"]}
    r = client.post(f"{BASE}/{b.id}/calls", json={"op": "skill_run", "args": args}, headers=me)
    assert refusal(r) == (422, "invalid_op") and "browser" in r.json()["detail"]
    sent = client.post(f"{AGENT}/{m.id}/calls", json={"op": "skill_run", "args": args, "timeout_seconds": 75}, headers=me).json()
    (call,) = m.poll().json()["calls"]
    assert call == {"id": sent["id"], "op": "skill_run", "args": args}
    out = {"exit_code": 0, "stdout": "Sunny\n", "stderr": ""}
    assert m.result(call["id"], ok=True, result=out).status_code == 204
    assert client.get(f"{AGENT}/calls/{sent['id']}", headers=me).json() == {"id": sent["id"], "status": "done", "ok": True, "result": out}


# --- local folders: the Mac's folders, mac_folders and folder_call (the Mac protocol's "Local folders")

FOLDER = {"id": "f1", "name": "Reports", "path": "/Users/sam/Reports", "mode": "read", "description": "Quarterly reports"}


def revision(client: TestClient, me: dict[str, str]) -> int:
    return _revision(client, me)


@needs_db
def test_a_mac_says_its_folders_and_they_become_its_agents_mac_folders(client: TestClient, me: dict[str, str]) -> None:
    m = mac(client, me)
    m.poll()
    before = revision(client, me)
    written = [FOLDER, {**FOLDER, "id": "f-2_B", "name": "  Inbox  ", "mode": "write", "description": ""}]
    m.poll(folders=written)
    folders = [FOLDER, {**FOLDER, "id": "f-2_B", "name": "Inbox", "mode": "write", "description": ""}]
    (link,) = client.get(AGENT, headers=me).json()["links"]
    assert link["folders"] == folders
    assert agent_config(client, me, "studio-mac")["mac_folders"] == folders
    # Written once, through the agents' write path: one revision.
    assert revision(client, me) == before + 1
    # The same list again, or none said, writes nothing.
    m.poll(folders=written)
    m.poll()
    m.poll(folders="Reports")
    assert revision(client, me) == before + 1
    assert client.get(AGENT, headers=me).json()["links"][0]["folders"] == folders
    # Malformed entries are dropped, the order kept, a repeated id once, 20 at most.
    bad = [
        5, "f", {**FOLDER, "id": ""}, {**FOLDER, "id": "x" * 33}, {**FOLDER, "id": "a b"}, {**FOLDER, "id": 7},
        {**FOLDER, "name": ""}, {**FOLDER, "name": "x" * 61}, {**FOLDER, "name": "two\nlines"}, {**FOLDER, "path": None},
        {**FOLDER, "path": "x" * 1001}, {**FOLDER, "path": "a\x00b"}, {**FOLDER, "mode": "all"}, {**FOLDER, "description": 5},
        {**FOLDER, "description": "x" * 1001},
    ]
    second = {k: v for k, v in {**FOLDER, "id": "f2"}.items() if k != "description"}
    m.poll(folders=[*bad, second, FOLDER, {**FOLDER, "name": "Again"}])
    assert agent_config(client, me, "studio-mac")["mac_folders"] == [{**second, "description": ""}, FOLDER]
    many = [{**FOLDER, "id": f"f{i}"} for i in range(25)]
    m.poll(folders=many)
    assert agent_config(client, me, "studio-mac")["mac_folders"] == many[:20]
    m.poll(folders=[])
    assert agent_config(client, me, "studio-mac")["mac_folders"] == []
    assert client.get(AGENT, headers=me).json()["links"][0]["folders"] == []
    # A big status body (20 folders with long paths and descriptions) is taken.
    big = [{**FOLDER, "id": f"f{i}", "path": "/" + "p" * 999, "description": "d" * 1000} for i in range(20)]
    assert m.poll(folders=big).status_code == 200
    assert len(agent_config(client, me, "studio-mac")["mac_folders"]) == 20
    # A browser says nothing of folders: none kept, none in its agent's settings.
    b = paired(client, me)
    b.poll(folders=[FOLDER])
    assert next(link for link in client.get(BASE, headers=me).json()["links"] if link["id"] == b.id)["folders"] == []
    assert "mac_folders" not in agent_config(client, me, "scout")


@needs_db
def test_mac_folders_are_the_links_whatever_a_save_sends(client: TestClient, me: dict[str, str]) -> None:
    m = mac(client, me)
    m.poll(folders=[FOLDER])
    config = agent_config(client, me, "studio-mac")
    for sent in ({k: v for k, v in config.items() if k != "mac_folders"}, {**config, "mac_folders": []}, {**config, "mac_folders": [{**FOLDER, "mode": "write"}]}):
        assert save(client, me, "studio-mac", {**sent, "instructions": "Be brief."}).status_code == 200
        assert agent_config(client, me, "studio-mac") == {**config, "instructions": "Be brief."}
    # A browser's agent and a plain one have none.
    paired(client, me)
    scout = agent_config(client, me, "scout")
    assert save(client, me, "scout", {**scout, "mac_folders": [FOLDER]}).status_code == 200
    assert agent_config(client, me, "scout") == scout
    _agent(client, me, "sky", {"label": "Sky", "mac_folders": [FOLDER]})
    assert agent_config(client, me, "sky") == {"label": "Sky"}
    # Unpaired, the agent stays without its Mac's folders.
    assert m.unpair().status_code == 204
    assert "mac_folders" not in agent_config(client, me, "studio-mac")


@needs_db
def test_a_mac_takes_folder_calls_and_a_browser_does_not(client: TestClient, me: dict[str, str]) -> None:
    m, b = mac(client, me), paired(client, me)
    m.poll(folders=[FOLDER])
    args = {"tool": "folder_read", "folder": "f1", "args": {"path": "q3.md"}}
    r = client.post(f"{BASE}/{b.id}/calls", json={"op": "folder_call", "args": args}, headers=me)
    assert refusal(r) == (422, "invalid_op") and "browser" in r.json()["detail"]
    sent = client.post(f"{AGENT}/{m.id}/calls", json={"op": "folder_call", "args": args, "timeout_seconds": 75}, headers=me).json()
    (call,) = m.poll().json()["calls"]
    assert call == {"id": sent["id"], "op": "folder_call", "args": args}
    out = {"text": "Q3 went well."}
    assert m.result(call["id"], ok=True, result=out).status_code == 204
    assert client.get(f"{AGENT}/calls/{sent['id']}", headers=me).json() == {"id": sent["id"], "status": "done", "ok": True, "result": out}


# --- a taken name, settled where the code is typed (2026-11-13) --------------------------------


@needs_db
def test_a_browsers_agent_with_no_browser_is_reconnected_and_keeps_everything(client: TestClient, me: dict[str, str]) -> None:
    first = paired(client, me, "Scout")
    config = agent_config(client, me, "scout")
    assert save(client, me, "scout", {**config, "label": "My Scout", "instructions": "Be brief."}).status_code == 200
    # Still paired with its browser: another browser of the same name cannot have it.
    second = Extension(client, "Scout")
    r = client.post(f"{BASE}/claim", json={"code": second.code, "reconnect": True}, headers=me)
    assert refusal(r) == (409, "name_taken") and (r.json()["agent"], r.json()["reconnect"]) == ({"name": "scout", "label": "My Scout"}, False)
    assert client.post(f"{BASE}/{first.id}/unpair", headers=me).status_code == 204
    # Unpaired, it may be: Wysebee says so, and the same code gives it the new browser.
    r = client.post(f"{BASE}/claim", json={"code": second.code}, headers=me)
    assert refusal(r) == (409, "name_taken") and r.json()["reconnect"] is True
    before = revision(client, me)
    r = client.post(f"{BASE}/claim", json={"code": second.code, "reconnect": True}, headers=me)
    assert r.status_code == 200, r.text
    assert r.json()["agent"] == {"name": "scout", "label": "My Scout"} and r.json()["link"]["id"] == second.id
    # Everything else is as it was; only its link changed, through a new revision.
    assert agent_config(client, me, "scout") == {**config, "label": "My Scout", "instructions": "Be brief.", "browser_link": second.id}
    assert revision(client, me) > before
    assert [item["name"] for item in agents_of(client, me)] == ["scout"]
    assert second.poll().json()["agent"] == {"name": "scout", "label": "My Scout"}
    assert client.post(f"{BASE}/{second.id}/calls", json={"op": "ping"}, headers=me).status_code == 200


@needs_db
def test_a_macs_agent_is_reconnected_by_a_name_of_the_persons_own_with_no_folders(client: TestClient, me: dict[str, str]) -> None:
    m = mac(client, me)
    m.poll(folders=[FOLDER])
    assert give(client, me, "studio-mac", "program", [make_tool(client, me, "program", GIVEN["program"][1])]).status_code == 200
    assert client.post(f"{AGENT}/{m.id}/unpair", headers=me).status_code == 204
    # A browser cannot be given a Mac's agent, under any name.
    b = Extension(client, "Studio Mac")
    r = client.post(f"{BASE}/claim", json={"code": b.code, "reconnect": True}, headers=me)
    assert refusal(r) == (409, "name_taken") and r.json()["reconnect"] is False
    # A Mac that typed another name is given it by the name the person types in Wysebee.
    again = Extension(client, "Laptop", kind="osx")
    r = client.post(f"{AGENT}/claim", json={"code": again.code, "name": "Studio Mac", "reconnect": True}, headers=me)
    assert r.status_code == 200, r.text
    assert r.json()["agent"] == {"name": "studio-mac", "label": "Studio Mac"}
    config = agent_config(client, me, "studio-mac")
    assert config == {"label": "Studio Mac", "paired_kind": "wysebee-osx", "agent_type_id": OSX_ID, "mac_link": again.id, "mac_folders": []}
    # It starts as any pairing does: nothing allowed there yet.
    answer = again.poll().json()
    assert answer["agent"] == {"name": "studio-mac", "label": "Studio Mac"} and answer["programs"] == [PROGRAM]
    (link,) = client.get(AGENT, headers=me).json()["links"]
    assert (link["approved"], link["folders"]) == ([], [])


@needs_db
def test_only_the_cloud_says_what_kind_an_agent_was_paired_as(client: TestClient, me: dict[str, str], migrated_db_url: str) -> None:
    e = paired(client, me)
    config = agent_config(client, me, "scout")
    # A save can neither change it nor drop it.
    for sent in ({**config, "paired_kind": "wysebee-osx", "agent_type_id": OSX_ID}, {k: v for k, v in config.items() if k != "paired_kind"}, {**config, "paired_kind": 5}):
        assert save(client, me, "scout", sent).status_code == 200
        assert agent_config(client, me, "scout") == config
    # Nor give one to a plain agent, which therefore cannot be reconnected.
    _agent(client, me, "plain", {"label": "Plain", "paired_kind": "wysebee-browser-chrome", "agent_type_id": CHROME_ID})
    assert "paired_kind" not in agent_config(client, me, "plain")
    # An agent paired before it was kept gets it from its link at its next save…
    with create_engine(migrated_db_url).begin() as conn:
        conn.execute(text("UPDATE user_agents SET config = config - 'paired_kind' WHERE user_id = :u AND name = 'scout'"), {"u": me["X-Test-User"]})
    assert "paired_kind" not in agent_config(client, me, "scout")
    assert save(client, me, "scout", {**config, "instructions": "Hi"}).status_code == 200
    assert agent_config(client, me, "scout")["paired_kind"] == "wysebee-browser-chrome"
    # …and keeps it once unpaired; a tool of its kind waits for it to be paired again.
    assert client.post(f"{BASE}/{e.id}/unpair", headers=me).status_code == 204
    unpaired = agent_config(client, me, "scout")
    assert unpaired["paired_kind"] == "wysebee-browser-chrome"
    wrong_kind(save(client, me, "scout", {**unpaired, "browser_devtools": True}))
    again = Extension(client, "Scout")
    assert client.post(f"{BASE}/claim", json={"code": again.code, "reconnect": True}, headers=me).status_code == 200
    assert save(client, me, "scout", {**agent_config(client, me, "scout"), "browser_devtools": True}).status_code == 200


# --- a phone: Wysebee Agent for Android (2026-11-15) ------------------------------------------


def phone(client: TestClient, me: dict[str, str], name: str = "Pixel Tester") -> Extension:
    """Wysebee Agent for Android, paired: its link, with its agent made."""
    p = Extension(client, name, kind="wysebee-android", browser="Pixel 9", extension_version="0.1.0")
    r = client.post(f"{AGENT}/claim", json={"code": p.code}, headers=me)
    assert r.status_code == 200, r.text
    p.claimed = r.json()
    return p


@needs_db
def test_typing_a_phones_code_makes_an_agent_for_that_phone(client: TestClient, me: dict[str, str]) -> None:
    p = phone(client, me)
    assert p.claimed["agent"] == {"name": "pixel-tester", "label": "Pixel Tester"}
    link = p.claimed["link"]
    assert (link["kind"], link["agent_type"], link["browser"], link["approved"], link["folders"]) == (
        "android", {"id": ANDROID_ID, "key": "wysebee-android", "name": "Wysebee Android Agent"}, "Pixel 9", [], [])
    # Its own field, no browser to give access to, no Mac's folders.
    assert agent_config(client, me, "pixel-tester") == {"label": "Pixel Tester", "android_link": p.id, "paired_kind": "wysebee-android", "agent_type_id": ANDROID_ID}
    # Its poll carries only calls, as a browser's does — whatever its status says.
    assert p.poll(paused=False, approved=["x"], folders=[]).json() == {"state": "paired", "agent": {"name": "pixel-tester", "label": "Pixel Tester"}, "calls": [], "cancelled": []}
    # Listed with the person's links.
    assert [(l["id"], l["kind"]) for l in client.get(AGENT, headers=me).json()["links"]] == [(p.id, "android")]


@needs_db
def test_a_phone_takes_its_types_ops_and_says_so_in_a_phones_words(client: TestClient, me: dict[str, str]) -> None:
    p = phone(client, me)
    p.poll()
    # Not a phone's op: a browser's or a Mac's.
    for op in ("browser_click", "local_run"):
        r = client.post(f"{AGENT}/{p.id}/calls", json={"op": op}, headers=me)
        assert refusal(r) == (422, "invalid_op") and r.json()["detail"] == AndroidPack.words[browser_links.NOT_AN_OP]
    # Its own go to it, and the answer comes back.
    with Browser(p, lambda op, args: {"ok": True, "result": {"op": op, **args}}) as at:
        r = client.post(f"{AGENT}/{p.id}/calls?wait=5", json={"op": "android_tap", "args": {"ref": 3}}, headers=me)
        assert r.json() == {"id": r.json()["id"], "status": "done", "ok": True, "result": {"op": "android_tap", "ref": 3}}
        assert [c["op"] for c in at.seen] == ["android_tap"]
    # Paused on the phone: refused at once, said of the phone.
    p.poll(paused=True)
    r = client.post(f"{AGENT}/{p.id}/calls", json={"op": "ping"}, headers=me)
    assert refusal(r) == (409, "browser_offline") and r.json()["detail"] == AndroidPack.words[browser_links.OFFLINE]
    # Taken by nobody: not reached, in a phone's words, and so unavailable to a tool.
    p.poll(paused=False)
    sub, state = me["X-Test-User"], client.app.state
    pixel = agent_item(client, me, "pixel-tester")["id"]
    out = client.portal.call(lambda: browser_links.run_op(state, sub, p.id, "ping", {}, agent=pixel))
    assert out == {"ok": False, "error": AndroidPack.words[browser_links.NOT_REACHED], "unavailable": True}
    # Taken, never answered: timed out, said of the phone.
    with Browser(p, lambda op, args: None):
        late = client.portal.call(lambda: browser_links.run_op(state, sub, p.id, "ping", {}, agent=pixel, timeout_seconds=0.5))
    assert late["ok"] is False and late["unavailable"] is False and late["error"].startswith("The phone did not finish this")
    # Unpaired: the phone hears it in a phone's words.
    assert client.post(f"{AGENT}/{p.id}/unpair", headers=me).status_code == 204
    r = p.poll()
    assert refusal(r) == (401, "unpaired")


@needs_db
def test_only_the_claim_sets_an_agents_phone_and_unpairing_leaves_the_agent(client: TestClient, me: dict[str, str]) -> None:
    p, b = phone(client, me), paired(client, me)
    config = agent_config(client, me, "pixel-tester")
    for sent in ({k: v for k, v in config.items() if k != "android_link"}, {**config, "android_link": "0" * 32}, {**config, "android_link": b.id}, {**config, "browser_link": p.id}):
        assert save(client, me, "pixel-tester", {**sent, "instructions": "Be brief."}).status_code == 200
        assert agent_config(client, me, "pixel-tester") == {**config, "instructions": "Be brief."}
    # A browser's agent cannot be given the phone.
    scout = agent_config(client, me, "scout")
    assert save(client, me, "scout", {**scout, "android_link": p.id}).status_code == 200
    assert agent_config(client, me, "scout") == scout
    # Unpaired from the phone: the agent stays, without it, keeping its kind to be reconnected.
    assert p.unpair().status_code == 204
    assert agent_config(client, me, "pixel-tester") == {"label": "Pixel Tester", "paired_kind": "wysebee-android", "agent_type_id": ANDROID_ID, "instructions": "Be brief."}
    # Reconnected from a phone, never from a browser.
    again = Extension(client, "Pixel Tester", kind="wysebee-android")
    assert client.post(f"{AGENT}/claim", json={"code": again.code, "reconnect": True}, headers=me).status_code == 200
    assert agent_config(client, me, "pixel-tester")["android_link"] == again.id
    assert again.unpair().status_code == 204
    other = Extension(client, "Pixel Tester")
    r = client.post(f"{AGENT}/claim", json={"code": other.code, "reconnect": True}, headers=me)
    assert refusal(r) == (409, "name_taken") and r.json()["reconnect"] is False


# --- pairing by a QR code (2026-11-17) --------------------------------------------------------


def scan(client: TestClient, token: str, name: str = "Pixel Scan", kind: str = "wysebee-android", **more) -> tuple[object, str]:
    """A device starting to pair with a QR code's offer: the answer, and its secret."""
    s = secret()
    r = client.post(f"{AGENT}/start", json={"kind": kind, "name": name, "secret_hash": hashlib.sha256(s.encode()).hexdigest(), "browser": "Pixel 9", "extension_version": "0.2.0", "offer": token, **more})
    return r, s


@needs_db
def test_a_device_that_scans_the_qr_code_is_paired_at_once(client: TestClient, me: dict[str, str]) -> None:
    offered = client.post(f"{AGENT}/offers", headers=me)
    assert offered.status_code == 200 and set(offered.json()) == {"id", "token", "expires_in", "cloud"} and offered.json()["expires_in"] == 600
    # The QR code names this cloud's public address, for the device to pair there.
    assert offered.json()["cloud"] == "http://testserver"
    assert client.post(f"{AGENT}/offers", headers={**me, "X-Forwarded-Proto": "https", "X-Forwarded-Host": "wysebee.example"}).json()["cloud"] == "https://wysebee.example"
    offer_id, token = offered.json()["id"], offered.json()["token"]
    assert client.get(f"{AGENT}/offers/{offer_id}", headers=me).json()["state"] == "waiting"
    r, s = scan(client, token)
    assert r.status_code == 200, r.text
    link_id = r.json()["id"]
    assert r.json() == {"id": link_id, "code": None, "expires_in": 0, "agent": {"name": "pixel-scan", "label": "Pixel Scan"}}
    # The device is paired, its agent made as a typed code makes it.
    polled = client.post(f"{AGENT}/{link_id}/poll", json={"status": {}}, headers=ext(s)).json()
    assert polled == {"state": "paired", "agent": {"name": "pixel-scan", "label": "Pixel Scan"}, "calls": [], "cancelled": []}
    assert agent_config(client, me, "pixel-scan")["android_link"] == link_id
    # Wysebee hears which agent; the code works once.
    assert client.get(f"{AGENT}/offers/{offer_id}", headers=me).json() == {"state": "paired", "agent": {"name": "pixel-scan", "label": "Pixel Scan"}}
    again, _ = scan(client, token, "Pixel Again")
    assert refusal(again) == (404, "offer_unknown")
    # Anything that is not an offer's token, and someone else's offer, are no offer.
    for bad in ("x", 5, "A" * 43):
        assert refusal(scan(client, bad)[0]) == (404, "offer_unknown")
    assert refusal(client.get(f"{AGENT}/offers/{offer_id}", headers={"X-Test-User": "someone-else"})) == (404, "offer_unknown")
    assert refusal(client.get(f"{AGENT}/offers/{'0' * 32}", headers=me)) == (404, "offer_unknown")


@needs_db
def test_a_name_refused_frees_the_qr_code_for_another(client: TestClient, me: dict[str, str]) -> None:
    phone(client, me, "Taken")
    token = client.post(f"{AGENT}/offers", headers=me).json()["token"]
    for name, code in (("Taken", "name_taken"), ("Pairing", "reserved")):
        r, _ = scan(client, token, name)
        assert refusal(r) == ((409 if code == "name_taken" else 422), code)
    # Nothing is left of the refused starts: no link waits.
    assert [l["agent"] for l in client.get(AGENT, headers=me).json()["links"]] == ["taken"]
    r, _ = scan(client, token, "Fresh Name")
    assert r.status_code == 200 and r.json()["agent"]["name"] == "fresh-name"
    # A kind that cannot pair takes nothing of the offer.
    other = client.post(f"{AGENT}/offers", headers=me).json()["token"]
    assert refusal(scan(client, other, kind="wysebee-nothing")[0]) == (422, "invalid_kind")
    assert scan(client, other, "Kept")[0].status_code == 200


@needs_db
def test_offers_run_out_are_few_and_are_purged(ui_dist: Path, migrated_db_url: str, tmp_path: Path, me: dict[str, str]) -> None:
    c = _client(ui_dist, migrated_db_url, tmp_path, browser_link_code_seconds=0.3, browser_link_starts_per_minute=10_000)
    try:
        made = [c.post(f"{AGENT}/offers", headers=me).json() for _ in range(6)]
        # At most five open: the oldest went.
        assert refusal(c.get(f"{AGENT}/offers/{made[0]['id']}", headers=me)) == (404, "offer_unknown")
        assert all(c.get(f"{AGENT}/offers/{m['id']}", headers=me).json()["state"] == "waiting" for m in made[1:])
        time.sleep(0.4)
        assert c.get(f"{AGENT}/offers/{made[5]['id']}", headers=me).json() == {"state": "expired", "expires_in": 0}
        assert refusal(scan(c, made[5]["token"])[0]) == (404, "offer_unknown")
        # Past their time once more, they are forgotten.
        time.sleep(0.4)
        c.portal.call(_purge, c.app.state)
        assert refusal(c.get(f"{AGENT}/offers/{made[5]['id']}", headers=me)) == (404, "offer_unknown")
        # A new offer drops those past their time too.
        assert c.post(f"{AGENT}/offers", headers=me).status_code == 200
    finally:
        c.__exit__(None, None, None)


@needs_db
def test_a_scan_may_reconnect_to_its_agent_with_no_phone_once_the_person_says_so(client: TestClient, me: dict[str, str]) -> None:
    old = phone(client, me, "Ada Android")
    offered = client.post(f"{AGENT}/offers", headers=me).json()
    token = offered["token"]
    # Still paired: taken, and not to be reconnected.
    r, _ = scan(client, token, "Ada Android")
    assert refusal(r) == (409, "name_taken") and r.json()["agent"] == {"name": "ada-android", "label": "Ada Android"} and r.json()["reconnect"] is False
    # Unpaired from its phone: the scan says it may be reconnected.
    assert old.unpair().status_code == 204
    r, _ = scan(client, token, "Ada Android")
    assert refusal(r) == (409, "name_taken") and r.json()["reconnect"] is True
    # Asked to, the device does not take the agent over by itself (2026-11-18): it gets a code,
    # as any device, and nothing is paired yet; the offer stays good.
    r, s = scan(client, token, "Ada Android", reconnect=True)
    assert r.status_code == 200 and set(r.json()) == {"id", "code", "expires_in"} and len(r.json()["code"]) == 8
    link_id, code = r.json()["id"], r.json()["code"]
    assert client.post(f"{AGENT}/{link_id}/poll", json={"status": {}}, headers=ext(s)).json()["state"] == "pending"
    assert "android_link" not in agent_config(client, me, "ada-android")
    assert client.get(f"{AGENT}/offers/{offered['id']}", headers=me).json()["state"] == "waiting"
    # The person types the code in Wysebee, is told the name is taken, and reconnects it there.
    taken = client.post(f"{AGENT}/claim", json={"code": code}, headers=me)
    assert refusal(taken) == (409, "name_taken") and taken.json()["reconnect"] is True
    made = client.post(f"{AGENT}/claim", json={"code": code, "reconnect": True}, headers=me)
    assert made.status_code == 200 and made.json()["agent"] == {"name": "ada-android", "label": "Ada Android"}
    assert agent_config(client, me, "ada-android")["android_link"] == link_id
    assert client.post(f"{AGENT}/{link_id}/poll", json={"status": {}}, headers=ext(s)).json()["state"] == "paired"
    # An offer not good (used, run out, never made) is no offer, reconnecting or not.
    for bad in ("A" * 43, "x"):
        assert refusal(scan(client, bad, "Ada Android", reconnect=True)[0]) == (404, "offer_unknown")
    # A browser's agent of that name is never a phone's to reconnect.
    paired(client, me, "Scout")
    other = client.post(f"{AGENT}/offers", headers=me).json()["token"]
    r, _ = scan(client, other, "Scout")
    assert refusal(r) == (409, "name_taken") and r.json()["reconnect"] is False

