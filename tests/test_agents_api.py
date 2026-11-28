"""The agents' routes (apps/cloud/agents.py) against the real test database: a person's agents
by id, renamed, deleted, Wysebee's own made when listed, and what a save keeps and refuses."""

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from apps.cloud import agents
from apps.cloud.models import UserAgent
from tests.cloud_agent_fixtures import _revision, client, me  # noqa: F401 — fixtures
from tests.conftest import needs_db

pytestmark = needs_db

BASE = "/api/v1/agents"


def make(client: TestClient, me: dict[str, str], name: str, **config) -> dict:
    r = client.post(BASE, json={"name": name, "config": {"label": name.title(), **config}}, headers=me)
    assert r.status_code == 201, r.text
    return r.json()


def listed(client: TestClient, me: dict[str, str]) -> list[str]:
    return [a["name"] for a in client.get(BASE, headers=me).json()["agents"]]


def test_the_list_makes_wysebees_own_once_and_puts_it_first(client: TestClient, me: dict[str, str]) -> None:
    make(client, me, "alpha")
    assert listed(client, me) == ["wysebee", "alpha"]
    assert listed(client, me) == ["wysebee", "alpha"]
    wysebee = client.get(BASE, headers=me).json()["agents"][0]
    assert (wysebee["config"], wysebee["description"]) == ({"label": "Wysebee", "browser_access": False}, None)
    assert client.get(f"{BASE}/{wysebee['id']}", headers=me).json()["name"] == "wysebee"


def test_an_agent_is_made_by_name_with_a_label(client: TestClient, me: dict[str, str]) -> None:
    before = _revision(client, me)
    made = client.post(BASE, json={"name": "helper", "config": {"label": "Helper", "instructions": "Be brief."}, "description": "Helps."}, headers=me)
    assert made.status_code == 201
    body = made.json()
    assert (body["name"], body["config"], body["description"]) == ("helper", {"label": "Helper", "instructions": "Be brief."}, "Helps.")
    assert str(uuid.UUID(body["id"])) == body["id"]
    # The change feed hears of it.
    assert _revision(client, me) > before
    for name, code in (("Helper", 422), ("9lives", 422), ("x" * 31, 422), (None, 422), ("helper", 409), ("wysebee", 409), ("pairing", 409)):
        r = client.post(BASE, json={"name": name, "config": {"label": "L"}}, headers=me)
        assert r.status_code == code, (name, r.text)
    for config in ({}, {"label": ""}, {"label": "a\nb"}, {"label": "x" * 61}, {"label": 5}):
        assert client.post(BASE, json={"name": "other", "config": config}, headers=me).status_code == 422
    assert client.post(BASE, json={"name": "other", "config": {"label": "O"}, "description": "x" * 2001}, headers=me).status_code == 422
    assert client.post(BASE, json={"name": "other", "config": {"label": "O", "instructions": "x" * 300_000}}, headers=me).status_code == 413
    # An empty description is none.
    assert client.post(BASE, json={"name": "other", "config": {"label": "O"}, "description": ""}, headers=me).json()["description"] is None


def test_at_most_twenty_agents_besides_wysebees(client: TestClient, me: dict[str, str]) -> None:
    listed(client, me)
    for i in range(agents.MAX_AGENTS):
        make(client, me, f"a{i}")
    r = client.post(BASE, json={"name": "one-more", "config": {"label": "One more"}}, headers=me)
    assert r.status_code == 409 and "20 agents" in r.json()["detail"]


def test_what_only_the_desktop_wrote_and_tools_of_their_own_are_dropped(client: TestClient, me: dict[str, str]) -> None:
    sent = {"home": "pc", "postgres_databases": [], "local_programs": [], "websites": ["https://x.test"], "instructions": "Hi."}
    made = make(client, me, "plain", **sent)
    assert made["config"] == {"label": "Plain", "instructions": "Hi."}
    assert client.put(f"{BASE}/{made['id']}", json={"config": {"label": "Plain", "browser": "Chrome"}}, headers=me).json()["config"] == {"label": "Plain"}


def test_a_tool_that_kind_of_agent_cannot_have_is_refused(client: TestClient, me: dict[str, str]) -> None:
    r = client.post(BASE, json={"name": "plain", "config": {"label": "Plain", "browser_devtools": True}}, headers=me)
    assert (r.status_code, r.json()["code"]) == (422, "wrong_agent_kind")
    made = make(client, me, "plain")
    r = client.put(f"{BASE}/{made['id']}", json={"config": {"label": "Plain", "browser_devtools": True}}, headers=me)
    assert (r.status_code, r.json()["code"]) == (422, "wrong_agent_kind")


def test_an_agent_is_renamed_and_changed_by_id(client: TestClient, me: dict[str, str]) -> None:
    a = make(client, me, "alpha", instructions="One.")
    make(client, me, "beta")
    url = f"{BASE}/{a['id']}"
    renamed = client.put(url, json={"name": "gamma"}, headers=me)
    assert renamed.status_code == 200
    # What is not sent is kept.
    assert (renamed.json()["name"], renamed.json()["config"]) == ("gamma", {"label": "Alpha", "instructions": "One."})
    assert client.put(url, json={"name": "gamma", "description": "Third."}, headers=me).json()["description"] == "Third."
    assert client.put(url, json={"config": {"label": "Gamma"}}, headers=me).json()["description"] == "Third."
    assert client.put(url, json={"description": ""}, headers=me).json()["description"] is None
    assert client.put(url, json={"name": "beta"}, headers=me).status_code == 409
    assert client.put(url, json={"name": "Bad"}, headers=me).status_code == 422
    assert client.put(url, json={"name": "wysebee"}, headers=me).status_code == 409
    assert listed(client, me) == ["wysebee", "beta", "gamma"]


def test_wysebees_own_is_neither_renamed_nor_deleted(client: TestClient, me: dict[str, str]) -> None:
    wysebee = client.get(BASE, headers=me).json()["agents"][0]
    url = f"{BASE}/{wysebee['id']}"
    assert client.put(url, json={"name": "other"}, headers=me).status_code == 400
    assert client.delete(url, headers=me).status_code == 400
    changed = client.put(url, json={"name": "wysebee", "config": {"label": "Wysebee", "instructions": "Hi."}}, headers=me)
    assert changed.json()["config"]["instructions"] == "Hi."


def test_another_persons_or_no_agent_is_not_there(client: TestClient, me: dict[str, str], migrated_db_url: str) -> None:
    other = {"X-Test-User": f"test-{uuid.uuid4().hex}"}
    try:
        theirs = make(client, other, "theirs")
        for url in (f"{BASE}/{theirs['id']}", f"{BASE}/{uuid.uuid4()}", f"{BASE}/{'-' * 36}"):
            assert client.get(url, headers=me).status_code == 404
            assert client.put(url, json={"name": "mine"}, headers=me).status_code == 404
            assert client.delete(url, headers=me).status_code == 404
        assert client.get(f"{BASE}/not-an-id", headers=me).status_code == 422
    finally:
        with create_engine(migrated_db_url).begin() as conn:
            for table in ("user_agents", "cloud_config_heads"):
                conn.execute(text(f"DELETE FROM {table} WHERE user_id = :u"), {"u": other["X-Test-User"]})


def test_deleting_an_agent_frees_its_name(client: TestClient, me: dict[str, str]) -> None:
    a = make(client, me, "alpha")
    wysebee = client.get(BASE, headers=me).json()["agents"][0]["id"]
    order = {"scope": "", "keys": [a["id"], wysebee]}
    assert client.put("/api/v1/menu-orders/agents", json=order, headers=me).status_code == 200
    assert client.delete(f"{BASE}/{a['id']}", headers=me).status_code == 204
    assert listed(client, me) == ["wysebee"]
    # Its place in the Agents menu's order goes with it.
    assert client.get("/api/v1/menu-orders/agents", headers=me).json()["orders"] == {"": [wysebee]}
    assert make(client, me, "alpha")["id"] != a["id"]


def test_finding_by_handle(client: TestClient, me: dict[str, str]) -> None:
    a = make(client, me, "alpha")
    sub = me["X-Test-User"]

    async def look() -> tuple:
        async with client.app.state.db.sessionmaker() as session:
            default = await agents.find(session, sub, "@wysebee")
            found = await agents.find(session, sub, " @alpha ")
            nothing = (await agents.find(session, sub, 5), await agents.find(session, sub, "nobody"), await agents.by_id(session, sub, None))
            names = (await agents.names(session, sub, set()), await agents.names(session, sub, {a["id"]}))
            made = await agents.stored(session, sub, "wysebee")
            again = await agents.stored(session, sub, "wysebee")
            await session.commit()
            return default, found, nothing, names, made.agent_id, again.agent_id

    default, found, nothing, names, made, again = client.portal.call(look)
    assert (default.agent_id, default.name, default.config) == (None, "wysebee", {"label": "Wysebee", "browser_access": False})
    assert found.agent_id == a["id"]
    assert nothing == (None, None, None)
    assert names == ({}, {a["id"]: "alpha"})
    assert made == again == client.get(BASE, headers=me).json()["agents"][0]["id"]


def test_a_clash_only_the_index_sees_is_a_409(client: TestClient, me: dict[str, str]) -> None:
    sub = me["X-Test-User"]

    async def clash() -> int:
        async with client.app.state.db.sessionmaker() as session:
            session.add(UserAgent(agent_id=str(uuid.uuid4()), user_id=sub, name="twin", config={}, created_by=sub, updated_by=sub))
            session.add(UserAgent(agent_id=str(uuid.uuid4()), user_id=sub, name="twin", config={}, created_by=sub, updated_by=sub))
            with pytest.raises(HTTPException) as refused:
                await agents._commit(session, "twin")
            return refused.value.status_code

    assert client.portal.call(clash) == 409
    assert "twin" not in listed(client, me)
