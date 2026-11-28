"""The tasks' routes (apps/cloud/tasks.py) against the real test database: a person's tasks by
id, renamed, scheduled, deleted with their schedule and runs."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from apps.cloud import tasks
from apps.cloud.models import UserTask, UserTaskRun
from tests.cloud_agent_fixtures import _revision, client, me  # noqa: F401 — fixtures
from tests.conftest import needs_db

pytestmark = needs_db

BASE = "/api/v1/tasks"
HOURLY = {"type": "recurring", "repeat": "hours", "every": 1}


def make(client: TestClient, me: dict[str, str], name: str, **fields) -> dict:
    r = client.post(BASE, json={"name": name, **fields}, headers=me)
    assert r.status_code == 201, r.text
    return r.json()


def test_a_task_is_made_with_its_fields_and_listed_by_name(client: TestClient, me: dict[str, str]) -> None:
    before = _revision(client, me)
    made = make(client, me, " Stretch ", prompt=" Stand up. ", description="", label="Body", browser_access=False, trigger=HOURLY, ignored=1)
    assert str(uuid.UUID(made["id"])) == made["id"]
    assert {k: made[k] for k in ("name", "prompt", "description", "label", "browser_access", "browser", "browser_devtools", "trigger", "steps", "last_run", "location")} == {
        "name": "Stretch", "prompt": "Stand up.", "description": "", "label": "Body", "browser_access": False, "browser": "",
        "browser_devtools": False, "trigger": HOURLY, "steps": [], "last_run": None, "location": "cloud",
    }
    assert datetime.fromisoformat(made["next_run_at"]) > datetime.now(UTC) + timedelta(minutes=59)
    assert _revision(client, me) > before
    make(client, me, "Alpha")
    rows = client.get(BASE, headers=me).json()["rows"]
    assert [(r["name"], r["trigger"]) for r in rows] == [("Alpha", {"type": "manual"}), ("Stretch", HOURLY)]
    assert rows[0]["next_run_at"] is None
    assert client.get(f"{BASE}/{made['id'].replace('-', '')}", headers=me).json()["name"] == "Stretch"


def test_what_a_task_may_be(client: TestClient, me: dict[str, str]) -> None:
    make(client, me, "Taken")
    for body, code in (
        ({"name": ""}, 422), ({"name": "x" * 201}, 422), ({"name": 5}, 422), ({"name": "Taken"}, 409),
        ({"name": "t", "prompt": "x" * 50_001}, 422), ({"name": "t", "label": 3}, 422), ({"name": "t", "browser_access": "yes"}, 422),
        ({"name": "t", "steps": "all"}, 422), ({"name": "t", "steps": [{"agent_id": 1, "tool": "x"}]}, 422),
        ({"name": "t", "steps": [{"tool": "x", "arguments": []}]}, 422), ({"name": "t", "steps": [{"tool": "x"}] * 11}, 422),
        ({"name": "t", "trigger": {"type": "recurring", "repeat": "hours", "every": 0}}, 422),
        ({"name": "t", "trigger": {"type": "once", "at": "2020-01-01T00:00:00+00:00"}}, 422),
    ):
        r = client.post(BASE, json=body, headers=me)
        assert r.status_code == code, (body, r.text)
    assert client.post(BASE, json=["not", "an", "object"], headers=me).status_code == 422
    steps = [{"agent_id": None, "tool": "load_skill", "arguments": {"name": "x"}}]
    assert make(client, me, "Stepped", steps=steps, prompt=None)["steps"] == steps


def test_a_task_is_changed_and_renamed_by_id(client: TestClient, me: dict[str, str]) -> None:
    at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    made = make(client, me, "Once", prompt="Remember.", label="L", trigger={"type": "once", "at": at})
    url = f"{BASE}/{made['id']}"
    renamed = client.put(url, json={"name": "Reminder", "label": ""}, headers=me)
    assert renamed.status_code == 200
    body = renamed.json()
    # What is not sent is kept; an emptied field is dropped; the trigger sent back as stored is not checked again.
    assert (body["id"], body["name"], body["prompt"], body["label"], body["trigger"]) == (made["id"], "Reminder", "Remember.", "", made["trigger"])
    assert client.put(url, json={"trigger": made["trigger"], "prompt": "Again."}, headers=me).json()["prompt"] == "Again."
    assert client.put(url, json={"trigger": HOURLY}, headers=me).json()["trigger"] == HOURLY
    assert client.put(url, json={"trigger": {"type": "manual"}}, headers=me).json()["next_run_at"] is None
    make(client, me, "Other")
    assert client.put(url, json={"name": "Other"}, headers=me).status_code == 409
    assert client.put(url, json={"name": " "}, headers=me).status_code == 422
    assert client.put(url, json="text", headers=me).status_code == 422


def test_deleting_a_task_deletes_its_schedule_and_runs(client: TestClient, me: dict[str, str]) -> None:
    made = make(client, me, "Hourly", trigger=HOURLY)
    kept = make(client, me, "Kept")
    order = {"scope": "", "keys": [made["id"], kept["id"]]}
    assert client.put("/api/v1/menu-orders/tasks", json=order, headers=me).status_code == 200

    async def ran() -> None:
        async with client.app.state.db.sessionmaker() as session:
            session.add(UserTaskRun(user_id=me["X-Test-User"], run_id=uuid.uuid4().hex, task_id=made["id"], status="succeeded"))
            await session.commit()

    client.portal.call(ran)
    assert len(client.get(f"/api/v1/data/task_runs?task_id={made['id']}", headers=me).json()["rows"]) == 1
    assert client.delete(f"{BASE}/{made['id']}", headers=me).status_code == 204
    assert client.get("/api/v1/task-schedules", headers=me).json()["schedules"] == {}
    assert client.get(f"/api/v1/data/task_runs?task_id={made['id']}", headers=me).json()["rows"] == []
    # Its place in the Tasks menu's order goes with it.
    assert client.get("/api/v1/menu-orders/tasks", headers=me).json()["orders"] == {"": [kept["id"]]}
    assert client.delete(f"{BASE}/{made['id']}", headers=me).status_code == 404


def test_another_persons_or_no_task_is_not_there(client: TestClient, me: dict[str, str], migrated_db_url: str) -> None:
    other = {"X-Test-User": f"test-{uuid.uuid4().hex}"}
    try:
        theirs = make(client, other, "Theirs")
        for url in (f"{BASE}/{theirs['id']}", f"{BASE}/{uuid.uuid4()}", f"{BASE}/{'-' * 36}"):
            assert client.get(url, headers=me).status_code == 404
            assert client.put(url, json={"name": "Mine"}, headers=me).status_code == 404
            assert client.delete(url, headers=me).status_code == 404
        assert client.get(f"{BASE}/not-a-task", headers=me).status_code == 422
        assert client.get(BASE, headers=me).json() == {"rows": []}
    finally:
        with create_engine(migrated_db_url).begin() as conn:
            for table in ("user_tasks", "cloud_config_heads"):
                conn.execute(text(f"DELETE FROM {table} WHERE user_id = :u"), {"u": other["X-Test-User"]})


def test_finding_and_a_clash_only_the_index_sees(client: TestClient, me: dict[str, str]) -> None:
    sub = me["X-Test-User"]

    async def look() -> tuple:
        async with client.app.state.db.sessionmaker() as session:
            return await tasks.of(session, sub, None), await tasks.of(session, sub, "nope")

    assert client.portal.call(look) == (None, None)

    async def clash() -> int:
        async with client.app.state.db.sessionmaker() as session:
            session.add(UserTask(task_id=str(uuid.uuid4()), user_id=sub, name="Twin", config={}, created_by=sub, updated_by=sub))
            session.add(UserTask(task_id=str(uuid.uuid4()), user_id=sub, name="Twin", config={}, created_by=sub, updated_by=sub))
            with pytest.raises(HTTPException) as refused:
                await tasks.commit(session, "Twin")
            return refused.value.status_code

    assert client.portal.call(clash) == 409
    assert client.get(BASE, headers=me).json() == {"rows": []}
