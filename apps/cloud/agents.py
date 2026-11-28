"""A person's agents (:class:`~apps.cloud.models.UserAgent`, migration 0063), kept here and
edited by the website.

An agent is ``{id, name, config, description}``: ``id`` a UUID, what everything links it by —
its skills (apps/cloud/skills.py), its tools (apps/cloud/user_tools.py), its paired browser,
Mac or phone (apps/cloud/browser_links.py), tasks' steps, runs and notifications — so it can be
renamed; ``name`` its @handle, unique per person; ``config`` its settings. **Wysebee's own
agent** is a row too, named ``wysebee``: made the first time the agents are listed (or it is
otherwise needed), never renamed or deleted. Until it is made, what reads it by handle gets
its defaults (:func:`find`).

The routes (a signed-in caller, who sees only their own; another's is a 404):

- ``GET /agents`` — ``{"agents": [...]}``, Wysebee's first, then by name.
- ``POST /agents`` ``{name, config, description?}`` — 201 the new agent; a name taken is a 409,
  and so is a 21st agent (:data:`MAX_AGENTS`, Wysebee's not counted).
- ``GET|PUT|DELETE /agents/{agent_id}`` — a put sends what changes (``name`` renames it);
  Wysebee's own cannot be renamed or deleted (400). Deleting it deletes its links to its
  skills, tools and device (the device hears on its next poll).

**A save** (:func:`save`, also the pairing's and a Mac's poll's) keeps what only the cloud
decides — which device it is paired with (``browser_links.keep_link``) — drops what are tools
of their own (``user_tools.without_moved``), is refused (422 ``{"detail", "code":
"wrong_agent_kind"}``) when it adds a tool that kind of agent cannot have
(apps/cloud/tool_kinds.py), and bumps the person's revision, so the change feed
(apps/cloud/config_events.py) tells their open pages.
"""

import json
import re
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.cloud import agent_types, changes, menu_orders, tool_kinds, user_tools
from apps.cloud.auth import User, current_user
from apps.cloud.config import Settings
from apps.cloud.deps import get_session
from apps.cloud.models import UserAgent
from apps.cloud.people import Person, named, of_rows, stamp

router = APIRouter(prefix="/v1", tags=["Agents"])

WYSEBEE = "wysebee"
WYSEBEE_CONFIG: dict[str, Any] = {"label": "Wysebee", "browser_access": False}
NAME = re.compile(r"^[a-z][a-z0-9_-]{0,29}$")
NAME_RULE = "An agent's name must start with a lowercase letter and hold only lowercase letters, digits, '-' or '_' (30 at most)."
# Handles the website's addresses use (and the pairing refuses).
RESERVED = ("pairing", "camera")
MAX_AGENTS = 20
MAX_LABEL = 60
MAX_DESCRIPTION = 2000

AgentId = Path(pattern=r"^[0-9a-fA-F-]{32,36}$")


class AgentIn(BaseModel):
    name: str | None = None
    config: dict[str, Any] | None = None
    description: str | None = None


class AgentOut(BaseModel):
    id: str
    name: str
    config: dict[str, Any]
    description: str | None
    created_at: datetime
    updated_at: datetime
    # Who made it and who changed it last (apps/cloud/people.py).
    created_by: Person
    updated_by: Person


class AgentList(BaseModel):
    agents: list[AgentOut]


def out(row: UserAgent, who: dict[str, Person]) -> AgentOut:
    return AgentOut(
        id=row.agent_id, name=row.name, config=dict(row.config or {}), description=row.description,
        created_at=row.created_at, updated_at=row.updated_at,
        created_by=named(who, row.created_by), updated_by=named(who, row.updated_by),
    )


async def answer(session: AsyncSession, row: UserAgent) -> AgentOut:
    return out(row, await of_rows(session, [row]))


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


# --- reading ------------------------------------------------------------------------------


async def all_of(session: AsyncSession, user_id: str) -> list[UserAgent]:
    """The person's agents, Wysebee's first, then by name."""
    rows = (await session.scalars(select(UserAgent).where(UserAgent.user_id == user_id))).all()
    return sorted(rows, key=lambda r: (r.name != WYSEBEE, r.name))


async def by_id(session: AsyncSession, user_id: str, agent_id: str | None) -> UserAgent | None:
    """The person's agent of that id, or None."""
    agent_id = _uuid(agent_id) if agent_id else None
    if agent_id is None:
        return None
    row = await session.get(UserAgent, agent_id, populate_existing=True)
    return row if row is not None and row.user_id == user_id else None


async def find(session: AsyncSession, user_id: str, name: Any) -> UserAgent | None:
    """The person's agent with that @handle (an ``@`` before it allowed), or None — Wysebee's
    own as its defaults, not stored, when it has no row yet (its ``agent_id`` None)."""
    if not isinstance(name, str):
        return None
    name = name.strip().removeprefix("@")
    row = await session.scalar(select(UserAgent).where(UserAgent.user_id == user_id, UserAgent.name == name))
    if row is None and name == WYSEBEE:
        return UserAgent(agent_id=None, user_id=user_id, name=WYSEBEE, config=dict(WYSEBEE_CONFIG), description=None)
    return row


async def wysebee(session: AsyncSession, user_id: str) -> UserAgent:
    """Wysebee's own agent, made if it has none (in the caller's transaction)."""
    await session.execute(
        insert(UserAgent).values(agent_id=str(uuid.uuid4()), user_id=user_id, name=WYSEBEE, config=WYSEBEE_CONFIG, created_by=user_id, updated_by=user_id)
        .on_conflict_do_nothing(index_elements=["user_id", "name"])
    )
    row = await session.scalar(select(UserAgent).where(UserAgent.user_id == user_id, UserAgent.name == WYSEBEE))
    assert row is not None
    return row


async def stored(session: AsyncSession, user_id: str, name: Any) -> UserAgent | None:
    """As :func:`find`, but Wysebee's own made when it has no row (in the caller's transaction)."""
    row = await find(session, user_id, name)
    return await wysebee(session, user_id) if row is not None and row.agent_id is None else row


async def names(session: AsyncSession, user_id: str, ids: set[str]) -> dict[str, str]:
    """The handles of the person's agents of these ids."""
    if not ids:
        return {}
    rows = (await session.execute(select(UserAgent.agent_id, UserAgent.name).where(UserAgent.user_id == user_id, UserAgent.agent_id.in_(ids)))).all()
    return {r.agent_id: r.name for r in rows}


# --- writing ------------------------------------------------------------------------------


def _check_config(settings: Settings, config: dict[str, Any]) -> None:
    if len(json.dumps(config, separators=(",", ":"), ensure_ascii=False).encode()) > settings.config_item_max_bytes:
        raise HTTPException(status_code=413, detail=f"An agent's settings are larger than {settings.config_item_max_bytes} bytes.")
    label = config.get("label")
    if label is not None and (not isinstance(label, str) or not label.strip() or len(label) > MAX_LABEL or "\n" in label):
        raise HTTPException(status_code=422, detail=f"An agent's label must be one line of 1 to {MAX_LABEL} characters.")


async def _bump(session: AsyncSession, user_id: str) -> None:
    """Tell the person's open pages (the change feed) that something changed."""
    await changes.bump(session, user_id)


async def save(
    session: AsyncSession, settings: Settings, user_id: str, row: UserAgent | None, *,
    name: str, config: dict[str, Any], description: str | None = None, by: str | None = None,
) -> UserAgent:
    """Make (``row`` None) or change one of the person's agents, in the caller's transaction
    (the module's docstring says what a save keeps and refuses), as ``by`` — who is saving
    (a user id), else its owner: a pairing or a Mac's poll. The name is not checked
    against :data:`NAME` here: the callers do."""
    from apps.cloud import browser_links

    await _bump(session, user_id)
    agent_id = row.agent_id if row is not None else str(uuid.uuid4())
    config = user_tools.without_moved(config)
    config = await browser_links.keep_link(session, user_id, agent_id, row, config)
    _check_config(settings, config)
    types = await agent_types.all_types(session)
    why = tool_kinds.refusal(dict(row.config or {}) if row is not None else None, config, types)
    if why is not None:
        raise browser_links.Refused(422, tool_kinds.CODE, why)
    taken = await session.scalar(select(UserAgent.agent_id).where(UserAgent.user_id == user_id, UserAgent.name == name, UserAgent.agent_id != agent_id))
    if taken is not None:
        raise HTTPException(status_code=409, detail=f"There is already an agent named '{name}'.")
    renamed = row is not None and row.name != name
    if row is None:
        row = UserAgent(agent_id=agent_id, user_id=user_id, name=name, config=config, description=description)
        stamp(row, by or user_id, made=True)
        session.add(row)
    else:
        row.name, row.config, row.description, row.updated_at = name, config, description, func.now()
        stamp(row, by or user_id)
    await session.flush()
    if renamed:
        # A Mac's poll says the agent's name: said at once.
        await browser_links.wake_for_agent(session, user_id, agent_id)
    return row


async def _commit(session: AsyncSession, name: str) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        # The name index caught a clash the check could not see: a 409, never a 500.
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"There is already an agent named '{name}'.") from exc


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _name(value: Any) -> str:
    if not isinstance(value, str) or not NAME.match(value):
        raise HTTPException(status_code=422, detail=NAME_RULE)
    if value in RESERVED or value == WYSEBEE:
        raise HTTPException(status_code=409, detail=f"There is already an agent named '{value}'.")
    return value


def _description(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if len(value) > MAX_DESCRIPTION or "\x00" in value:
        raise HTTPException(status_code=422, detail=f"An agent's description is at most {MAX_DESCRIPTION} characters.")
    return value


async def _mine(session: AsyncSession, user: User, agent_id: str) -> UserAgent:
    row = await by_id(session, user.sub, agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="There is no such agent.")
    return row


# --- routes -------------------------------------------------------------------------------


@router.get("/agents", response_model=AgentList)
async def list_agents(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> AgentList:
    await wysebee(session, user.sub)
    rows = await all_of(session, user.sub)
    await session.commit()
    who = await of_rows(session, rows)
    return AgentList(agents=[out(r, who) for r in rows])


@router.post("/agents", response_model=AgentOut, status_code=201)
async def create_agent(body: AgentIn, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> AgentOut:
    name = _name(body.name)
    config = dict(body.config or {})
    if not isinstance(config.get("label"), str):
        raise HTTPException(status_code=422, detail=f"An agent's label must be one line of 1 to {MAX_LABEL} characters.")
    count = await session.scalar(select(func.count()).select_from(UserAgent).where(UserAgent.user_id == user.sub, UserAgent.name != WYSEBEE))
    if (count or 0) >= MAX_AGENTS:
        raise HTTPException(status_code=409, detail=f"You have {MAX_AGENTS} agents already: delete one to add another.")
    row = await save(session, _settings(request), user.sub, None, name=name, config=config, description=_description(body.description), by=user.sub)
    await _commit(session, name)
    await session.refresh(row)
    return await answer(session, row)


@router.get("/agents/{agent_id}", response_model=AgentOut)
async def get_agent(agent_id: str = AgentId, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> AgentOut:
    return await answer(session, await _mine(session, user, agent_id))


@router.put("/agents/{agent_id}", response_model=AgentOut)
async def put_agent(
    body: AgentIn, request: Request, agent_id: str = AgentId, user: User = Depends(current_user), session: AsyncSession = Depends(get_session),
) -> AgentOut:
    row = await _mine(session, user, agent_id)
    name = row.name
    if body.name is not None and body.name != row.name:
        if row.name == WYSEBEE:
            raise HTTPException(status_code=400, detail="Wysebee's own agent cannot be renamed.")
        name = _name(body.name)
    config = dict(body.config) if body.config is not None else dict(row.config or {})
    description = _description(body.description) if body.description is not None else row.description
    await save(session, _settings(request), user.sub, row, name=name, config=config, description=description, by=user.sub)
    await _commit(session, name)
    await session.refresh(row)
    return await answer(session, row)


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: str = AgentId, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> Response:
    from apps.cloud import browser_links

    row = await _mine(session, user, agent_id)
    if row.name == WYSEBEE:
        raise HTTPException(status_code=400, detail="Wysebee's own agent cannot be deleted.")
    # Its device hears on its next poll; its links to skills and tools go with it (cascade).
    await browser_links.delete_for_agent(session, user.sub, row.agent_id)
    await menu_orders.drop_key(session, user.sub, "agents", "", row.agent_id)
    await _bump(session, user.sub)
    await session.delete(row)
    await session.commit()
    return Response(status_code=204)
