"""A person's tasks (:class:`~apps.cloud.models.UserTask`, migration 0065), kept here and
edited by the website and by their AI app (apps/cloud/task_runner.py's tools).

A task is answered as the website's Tasks page reads it: ``{id, name, prompt, description,
label, browser_access, browser, browser_devtools, trigger, steps, next_run_at, creator,
created_by, updated_by,
created_at, updated_at, last_run, location}``. ``id`` is a UUID, what its schedule
(``task_schedules``) and runs (``user_task_runs``) link by — they are deleted with it — so a
task can be renamed; ``name`` is unique per person.

The routes (a signed-in caller, who sees only their own; another's is a 404):

- ``GET /tasks`` — ``{"rows": [...]}`` by name, each with when it is next due.
- ``POST /tasks`` ``{name, trigger?, prompt?, description?, label?, browser_access?, browser?,
  browser_devtools?, steps?}`` — 201 the new task, made by the caller. ``creator`` is the
  email of who made it (``created_by``, ``{id, name, email}``, apps/cloud/people.py).
- ``GET|PUT|DELETE /tasks/{id}`` — a put sends what changes (``name`` renames it); a trigger
  sent as it is stored is not checked again (a timer already past stays).

A name is 1 to 200 characters (409 when the person has one so named); ``prompt`` at most
50,000, ``description`` 2,000, ``label`` and ``browser`` 200; an empty field is dropped.
A trigger is checked as the AI app's is (``task_runner.clean_trigger``). Every save works out
when it is next due (``task_runner.sync``) and bumps the person's revision, so the change
feed tells their open pages.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apps.cloud import changes, menu_orders
from apps.cloud.auth import User, current_user
from apps.cloud.deps import get_session
from apps.cloud.models import TaskSchedule, UserTask
from apps.cloud.people import Person, named, of_rows, stamp

router = APIRouter(prefix="/v1", tags=["Tasks"])

NAME_MAX = 200
LIMITS = {"prompt": 50_000, "description": 2_000, "label": 200, "browser": 200}
TEXTS = tuple(LIMITS)
SWITCHES = ("browser_access", "browser_devtools")
STEPS_MAX = 10

TaskId = Path(pattern=r"^[0-9a-fA-F-]{32,36}$")


def out(task: UserTask, who: dict[str, Person], next_run_at: datetime | None = None) -> dict[str, Any]:
    """A task as the Tasks page reads it."""
    made = named(who, task.created_by)
    c = task.config or {}
    trigger = c.get("trigger")
    return {
        "id": task.task_id,
        "name": task.name,
        "prompt": c.get("prompt") or "",
        "label": c.get("label") or "",
        "description": c.get("description") or "",
        "browser_access": c.get("browser_access") is not False,
        "browser": c.get("browser") or "",
        "browser_devtools": c.get("browser_devtools") is True,
        "trigger": trigger if isinstance(trigger, dict) else {"type": "manual"},
        "steps": c.get("steps") if isinstance(c.get("steps"), list) else [],
        "next_run_at": next_run_at.isoformat() if next_run_at else None,
        "creator": made.email or None,
        "created_by": made.model_dump(),
        "updated_by": named(who, task.updated_by).model_dump(),
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        # Runs are the Run tab's (``/data/task_runs``).
        "last_run": None,
        "location": "cloud",
    }


def clean_name(value: Any) -> str:
    name = value.strip() if isinstance(value, str) else ""
    if not name:
        raise HTTPException(status_code=422, detail="A task needs a name.")
    if len(name) > NAME_MAX:
        raise HTTPException(status_code=422, detail=f"A task's name is at most {NAME_MAX} characters.")
    return name


def _steps(value: Any) -> list[dict[str, Any]]:
    """Steps as the website sends them back — as they are stored (the AI app sets them up,
    apps/cloud/task_runner.py ``save_task``) — or a 422."""
    ok = isinstance(value, list) and len(value) <= STEPS_MAX and all(
        isinstance(s, dict) and isinstance(s.get("tool"), str) and (s.get("agent_id") is None or isinstance(s.get("agent_id"), str))
        and isinstance(s.get("arguments", {}), dict)
        for s in value
    )
    if not ok:
        raise HTTPException(status_code=422, detail=f"'steps' is a list of at most {STEPS_MAX} agent tool calls.")
    return value


def merged(sent: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """The task's settings: what was sent over what is stored, texts trimmed and checked;
    empty ones dropped."""
    config = dict(stored)
    for key in TEXTS:
        if key in sent:
            value = sent[key]
            if value is not None and not isinstance(value, str):
                raise HTTPException(status_code=422, detail=f"'{key}' must be text.")
            value = (value or "").strip()
            if len(value) > LIMITS[key]:
                raise HTTPException(status_code=422, detail=f"'{key}' is too long (max {LIMITS[key]} characters).")
            config[key] = value
    for key in SWITCHES:
        if key in sent:
            if not isinstance(sent[key], bool):
                raise HTTPException(status_code=422, detail=f"'{key}' must be true or false.")
            config[key] = sent[key]
    if "steps" in sent:
        config["steps"] = _steps(sent["steps"])
    return {k: v for k, v in config.items() if v not in (None, "", [], {})}


def trigger_of(raw: Any) -> dict[str, Any]:
    from apps.cloud import task_runner

    cleaned = task_runner.clean_trigger(raw, datetime.now(UTC))
    if isinstance(cleaned, str):
        raise HTTPException(status_code=422, detail=cleaned)
    return cleaned


# --- reading and writing, for the routes and the AI app's tools --------------------------


async def of(session: AsyncSession, user_id: str, task_id: Any) -> UserTask | None:
    """The person's task of that id (with or without its dashes), or None."""
    try:
        task_id = str(uuid.UUID(task_id)) if isinstance(task_id, str) else None
    except ValueError:
        return None
    task = await session.get(UserTask, task_id, populate_existing=True) if task_id else None
    return task if task is not None and task.user_id == user_id else None


async def all_of(session: AsyncSession, user_id: str) -> list[UserTask]:
    return list((await session.scalars(select(UserTask).where(UserTask.user_id == user_id).order_by(UserTask.name))).all())


async def due_of(session: AsyncSession, user_id: str) -> dict[str, datetime]:
    """When each of the person's tasks is next due."""
    rows = (await session.scalars(select(TaskSchedule).where(TaskSchedule.user_id == user_id))).all()
    return {r.task_id: r.next_run_at for r in rows}


async def save(
    session: AsyncSession, user_id: str, task: UserTask | None, *, name: str, config: dict[str, Any], by: str | None = None,
) -> UserTask:
    """Make (``task`` None) or change one of the person's tasks and work out when it is next
    due, in the caller's transaction, after bumping their revision; a name they already use
    is a 409. The caller commits (:func:`commit`)."""
    from apps.cloud import task_runner

    await changes.bump(session, user_id)
    task_id = task.task_id if task is not None else str(uuid.uuid4())
    taken = await session.scalar(select(UserTask.task_id).where(UserTask.user_id == user_id, UserTask.name == name, UserTask.task_id != task_id))
    if taken is not None:
        raise HTTPException(status_code=409, detail=f"A task named '{name}' already exists.")
    if task is None:
        task = UserTask(task_id=task_id, user_id=user_id, name=name, config=config)
        stamp(task, by or user_id, made=True)
        session.add(task)
    else:
        task.name, task.config, task.updated_at = name, config, datetime.now(UTC)
        stamp(task, by or user_id)
    await session.flush()
    await task_runner.sync(session, user_id, task_id, config)
    return task


async def remove(session: AsyncSession, user_id: str, task: UserTask) -> None:
    """Delete it — its schedule and runs with it, and its place in the Tasks menu's order —
    after bumping the person's revision."""
    await changes.bump(session, user_id)
    await menu_orders.drop_key(session, user_id, "tasks", "", task.task_id)
    await session.delete(task)


async def commit(session: AsyncSession, name: str) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        # The name index caught a clash the check could not see: a 409, never a 500.
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"A task named '{name}' already exists.") from exc


async def answer(session: AsyncSession, task: UserTask) -> dict[str, Any]:
    return out(task, await of_rows(session, [task]), (await due_of(session, task.user_id)).get(task.task_id))


async def _mine(session: AsyncSession, user: User, task_id: str) -> UserTask:
    task = await of(session, user.sub, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="No such task.")
    return task


def _body(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="The request body must be an object.")
    return value


# --- routes -------------------------------------------------------------------------------


@router.get("/tasks")
async def list_tasks(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    due = await due_of(session, user.sub)
    rows = await all_of(session, user.sub)
    who = await of_rows(session, rows)
    return {"rows": [out(t, who, due.get(t.task_id)) for t in rows]}


@router.post("/tasks", status_code=201)
async def create_task(request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    body = _body(await request.json())
    name = clean_name(body.get("name"))
    config = {**merged(body, {}), "trigger": trigger_of(body.get("trigger"))}
    task = await save(session, user.sub, None, name=name, config=config, by=user.sub)
    await commit(session, name)
    await session.refresh(task)
    return await answer(session, task)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str = TaskId, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    return await answer(session, await _mine(session, user, task_id))


@router.put("/tasks/{task_id}")
async def put_task(request: Request, task_id: str = TaskId, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    body = _body(await request.json())
    task = await _mine(session, user, task_id)
    stored = dict(task.config or {})
    name = clean_name(body["name"]) if "name" in body else task.name
    config = merged(body, {k: v for k, v in stored.items() if k != "trigger"})
    sent = body.get("trigger")
    unchanged = "trigger" not in body or sent == stored.get("trigger")
    config["trigger"] = stored.get("trigger") or {"type": "manual"} if unchanged else trigger_of(sent)
    await save(session, user.sub, task, name=name, config=config, by=user.sub)
    await commit(session, name)
    await session.refresh(task)
    return await answer(session, task)


@router.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: str = TaskId, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> Response:
    task = await _mine(session, user, task_id)
    await remove(session, user.sub, task)
    await session.commit()
    return Response(status_code=204)
