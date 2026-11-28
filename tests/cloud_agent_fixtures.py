"""Shared by the tests of agents living in Wysebee Cloud (test_cloud_postgres.py,
test_cloud_snowflake.py, test_cloud_agent_browser.py, test_cloud_agent_mac.py, test_agent_mcp.py
…): a client against the real test database with a key-encryption key configured, the caller
faked with X-Test-User, and helpers to save an agent and run its tools as Wysebee Assistant does
(apps/cloud/agent_chat/toolbox.py)."""

import base64
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import Header
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from apps.cloud import agents
from apps.cloud.agent_chat import toolbox, tools
from apps.cloud.auth import User, current_user
from tests.conftest import make_client

KEK = base64.b64encode(b"k" * 32).decode()


def _fake_user(x_test_user: str = Header()) -> User:
    return User(sub=x_test_user)


def _client(ui_dist: Path, url: str, tmp_path: Path, loaded=None, **settings) -> TestClient:
    c = make_client(ui_dist, loaded, database_url=url, skill_file_dir=tmp_path / "skill-files", **settings)
    c.__enter__()
    c.app.dependency_overrides[current_user] = _fake_user
    return c


@pytest.fixture
def client(ui_dist: Path, migrated_db_url: str, tmp_path: Path) -> Iterator[TestClient]:
    c = _client(ui_dist, migrated_db_url, tmp_path, ai_kek=KEK)
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def me(migrated_db_url: str) -> Iterator[dict[str, str]]:
    sub = f"test-{uuid.uuid4().hex}"
    yield {"X-Test-User": sub}
    with create_engine(migrated_db_url).begin() as conn:
        for table in ("agent_link_offers", "agent_skills", "user_skills", "agent_tools", "user_tools", "browser_calls", "browser_recordings", "browser_links", "ai_turn_counts", "relay_traffic", "mcp_hidden_agents", "user_notifications", "task_schedules", "user_task_runs", "user_agents", "user_tasks", "workspace_assistants", "user_workspaces", "cloud_config_heads", "cloud_skill_files", "menu_orders", "users"):
            conn.execute(text(f"DELETE FROM {table} WHERE user_id = :u"), {"u": sub})


def _agent(client: TestClient, me: dict[str, str], name: str, config: dict, description: str | None = None) -> str:
    """Make the agent named ``name``, or change it (Wysebee's own: ``wysebee``) — its id;
    ``config``'s ``skills``, skills' names, are given to it (apps/cloud/skills.py)."""
    config = dict(config)
    names = config.pop("skills", None)
    body = {"name": name, "config": config, "description": description}
    held = _agents(client, me).get(name)
    if held is not None:
        r = client.put(f"/api/v1/agents/{held}", json=body, headers=me)
        assert r.status_code == 200, r.text
    else:
        r = client.post("/api/v1/agents", json={**body, "config": {"label": name, **config}}, headers=me)
        assert r.status_code == 201, r.text
    if names is not None:
        _give(client, me, name, names)
    return r.json()["id"]


def _agents(client: TestClient, me: dict[str, str]) -> dict[str, str]:
    """The person's agents' ids, by handle (Wysebee's own made, if it has none)."""
    return {a["name"]: a["id"] for a in client.get("/api/v1/agents", headers=me).json()["agents"]}


def _skill(client: TestClient, me: dict[str, str], name: str, manifest: bytes | None = None, *, files: list[dict] | None = None, description: str | None = None) -> dict:
    """Make a skill as the website does (apps/cloud/skill_folders.py): made by name, its
    SKILL.md written (``manifest``, when given), then each of ``files`` (``[{path, id}]`` of
    files already uploaded to /skill-files, or ``[{path, data}]``). Answers the skill."""
    r = client.post("/api/v1/skills", json={"name": name, "description": description or f"{name}."}, headers=me)
    assert r.status_code == 201, r.text
    skill = r.json()
    for path, data in ([("SKILL.md", manifest)] if manifest is not None else []) + [(f["path"], _bytes(client, me, f)) for f in files or []]:
        r = _put_skill_file(client, me, skill["id"], path, data)
        assert r.status_code == 200, r.text
        skill = r.json()
    return skill


def _bytes(client: TestClient, me: dict[str, str], f: dict) -> bytes:
    return f["data"] if "data" in f else client.get(f"/api/v1/skill-files/{f['id']}", headers=me).content


def _put_skill_file(client: TestClient, me: dict[str, str], skill_id: str, path: str, data: bytes):
    """Write one file of a skill as an upload: its bytes."""
    return client.put(f"/api/v1/skills/{skill_id}/files/{path}", content=data, headers={**me, "Content-Type": "application/octet-stream"})


def _config(client: TestClient, me: dict[str, str], name: str) -> dict:
    """The settings of the agent named ``name``, as the server keeps them."""
    return client.get(f"/api/v1/agents/{_agent_id(client, me, name)}", headers=me).json()["config"]


def _save_config(client: TestClient, me: dict[str, str], name: str, config: dict):
    """Save the agent named ``name`` with these settings, as the website does."""
    return client.put(f"/api/v1/agents/{_agent_id(client, me, name)}", json={"config": config}, headers=me)


def _agent_id(client: TestClient, me: dict[str, str], name: str) -> str:
    return _agents(client, me)[name]


def _give(client: TestClient, me: dict[str, str], agent: str, names: list[str]):
    """Give the agent named ``agent`` the skills named ``names``, in order (a name with no
    skill is left out)."""
    ids = {s["name"]: s["id"] for s in client.get("/api/v1/skills", headers=me).json()["skills"]}
    wanted = list(dict.fromkeys(ids[n] for n in names if n in ids))
    r = client.put(f"/api/v1/agents/{_agent_id(client, me, agent)}/skills", json={"skill_ids": wanted}, headers=me)
    assert r.status_code == 200, r.text
    return r


def _box(client: TestClient, me: dict[str, str], agent: str) -> toolbox.Toolbox:
    """The agent's tools as Wysebee Assistant offers them."""
    state = client.app.state

    async def make() -> toolbox.Toolbox:
        async with state.db.sessionmaker() as session:
            found = await agents.find(session, me["X-Test-User"], agent)
            assert found is not None
            return await toolbox.toolbox(state, session, me["X-Test-User"], found)

    return client.portal.call(make)


def _offered(client: TestClient, me: dict[str, str], agent: str) -> set[str]:
    return _box(client, me, agent).offered


def _call(client: TestClient, me: dict[str, str], agent: str, name: str, arguments: dict | None = None) -> tools.Output:
    """One of the agent's tools, run as Wysebee Assistant's ``call_agent_tool`` runs it."""
    box = _box(client, me, agent)

    async def run() -> tools.Output:
        return await box.run({"name": name, "arguments": arguments or {}})

    return client.portal.call(run)


def _save_tool(client: TestClient, me: dict[str, str], kind: str, name: str = "", *, description: str = "", config: dict | None = None, secret: str | None = None, tool_id: str | None = None):
    """Make a tool (apps/cloud/user_tools.py), or change the one ``tool_id`` names."""
    body = {"kind": kind, "name": name, "description": description, "config": config or {}, **({"secret": secret} if secret is not None else {})}
    return client.put(f"/api/v1/tools/{tool_id}", json=body, headers=me) if tool_id else client.post("/api/v1/tools", json=body, headers=me)


def _tool_named(client: TestClient, me: dict[str, str], kind: str, name: str) -> dict | None:
    return next((t for t in client.get(f"/api/v1/tools?kind={kind}", headers=me).json()["tools"] if t["name"] == name), None)


def _give_tools(client: TestClient, me: dict[str, str], agent: str, kind: str, names: list[str]):
    """Give the agent named ``agent`` the ``kind`` tools named ``names``, in order."""
    ids = [_tool_named(client, me, kind, n)["id"] for n in names]
    return client.put(f"/api/v1/agents/{_agent_id(client, me, agent)}/tools", json={"kind": kind, "tool_ids": ids}, headers=me)


def _connect(client: TestClient, me: dict[str, str], agent: str, kind: str, name: str, flat: dict):
    """A connection as the old per-agent routes took it — ``flat`` its fields, ``password`` or
    ``secret`` among them — saved as a tool (made, or changed when the name is taken) and given
    to ``agent`` (made first when there is none) beside its others. Answers the save."""
    if agent not in _agents(client, me):
        _agent(client, me, agent, {"label": agent})
    flat = dict(flat)
    description = flat.pop("description", "")
    secret = flat.pop("password", flat.pop("secret", None))
    found = _tool_named(client, me, kind, name)
    saved = _save_tool(client, me, kind, name, description=description, config=flat, secret=secret, tool_id=found["id"] if found else None)
    if saved.status_code in (200, 201):
        mine = [t["name"] for t in client.get(f"/api/v1/agents/{_agent_id(client, me, agent)}/tools", headers=me).json()["tools"] if t["kind"] == kind]
        if name not in mine:
            _give_tools(client, me, agent, kind, [*mine, name])
    return saved


def _give_programs(client: TestClient, me: dict[str, str], agent: str, programs: list[dict]):
    """Give a Mac's agent these programs (``{name, description, command, args}``), each a
    ``program`` tool, made or changed by its name."""
    for p in programs:
        found = _tool_named(client, me, "program", p["name"])
        r = _save_tool(client, me, "program", p["name"], description=p["description"], config={"command": p["command"], "args": p.get("args", [])}, tool_id=found["id"] if found else None)
        assert r.status_code in (200, 201), r.text
    r = _give_tools(client, me, agent, "program", [p["name"] for p in programs])
    assert r.status_code == 200, r.text
    return r


def _revision(client: TestClient, me: dict[str, str]) -> int:
    """The person's change counter (apps/cloud/changes.py), which every agent or task save bumps."""
    from apps.cloud import changes

    async def read() -> int:
        async with client.app.state.db.sessionmaker() as session:
            return await changes.head(session, me["X-Test-User"])

    return client.portal.call(read)
