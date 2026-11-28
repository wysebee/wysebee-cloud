"""Menu orders: the order a person dragged a menu's items into (2026-11-28).

The menus beside the rail list their items by name until the person drags them into an order of
their own; that order is kept here, one list per menu and scope (``MenuOrder``, migration 0087).
It is the same for every menu, so a menu that gains dragging only names its scopes and keys:

- ``workspaces``: scope ``""`` holds the workspaces' ids; ``<workspace id>`` its top folder's
  items by name, and ``<workspace id>/<folder path>`` a folder's. A folder's items keep folders
  before files whatever the order; that is the menu's rule, not this store's.
- ``tasks``: scope ``""`` holds the person's task ids (apps/cloud/tasks.py).
- ``agents``: scope ``""`` holds the person's agent ids (apps/cloud/agents.py).

The routes:

- ``GET /menu-orders/{menu}`` — ``{orders: {scope: [key]}}``, every list the person has kept.
- ``PUT /menu-orders/{menu}`` ``{scope, keys}`` — keeps that list (an empty one forgets it);
  ``{scope, keys}``. A key or a scope is text; at most ``KEYS_MAX`` keys, each at most
  ``KEY_MAX`` characters, without repeats.

The lists only order: a key no item has any more is passed over, and an item no list names
follows the named ones, by name. Renaming, moving or deleting an item keeps its lists in step
where the item's own code calls the helpers below (apps/cloud/workspaces.py; a task's or an
agent's deletion drops its key).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.cloud.auth import User, current_user
from apps.cloud.deps import get_session
from apps.cloud.models import MenuOrder

router = APIRouter(prefix="/v1", tags=["Menu orders"])

KEYS_MAX = 2000
KEY_MAX = 400
SCOPE_MAX = 500

Menu = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]{0,39}$")]


class OrderBody(BaseModel):
    scope: str = Field(max_length=SCOPE_MAX)
    keys: list[Annotated[str, Field(min_length=1, max_length=KEY_MAX)]] = Field(max_length=KEYS_MAX)


@router.get("/menu-orders/{menu}")
async def get_orders(menu: Menu, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = (await session.scalars(select(MenuOrder).where(MenuOrder.user_id == user.sub, MenuOrder.menu == menu))).all()
    return {"orders": {row.scope: row.keys for row in rows}}


@router.put("/menu-orders/{menu}")
async def put_order(menu: Menu, body: OrderBody, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    if len(set(body.keys)) != len(body.keys):
        raise HTTPException(status_code=422, detail="A menu's order names each item once.")
    row = await session.get(MenuOrder, (user.sub, menu, body.scope))
    if not body.keys:
        if row is not None:
            await session.delete(row)
    elif row is None:
        session.add(MenuOrder(user_id=user.sub, menu=menu, scope=body.scope, keys=body.keys))
    else:
        row.keys, row.updated_at = body.keys, func.now()
    await session.commit()
    return {"scope": body.scope, "keys": body.keys}


# --- kept in step by the items' own code (the caller commits) ------------------------------


def _at_or_under(scope: str) -> Any:
    under = scope.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
    return or_(MenuOrder.scope == scope, MenuOrder.scope.like(under, escape="\\"))


async def forget(session: AsyncSession, user_id: str, menu: str, scope: str) -> None:
    """A list's owner gone (a folder, a workspace): its list, and those of everything in it."""
    await session.execute(delete(MenuOrder).where(MenuOrder.user_id == user_id, MenuOrder.menu == menu, _at_or_under(scope)))


async def move_scope(session: AsyncSession, user_id: str, menu: str, scope: str, to: str) -> None:
    """A list's owner moved or renamed: its list, and those of everything in it, go with it."""
    rows = (await session.scalars(select(MenuOrder).where(MenuOrder.user_id == user_id, MenuOrder.menu == menu, _at_or_under(scope)))).all()
    for row in rows:
        new = to + row.scope[len(scope) :]
        # A list already there (left by an item of that name) gives way.
        await session.execute(delete(MenuOrder).where(MenuOrder.user_id == user_id, MenuOrder.menu == menu, MenuOrder.scope == new))
        row.scope = new


async def rename_key(session: AsyncSession, user_id: str, menu: str, scope: str, key: str, to: str) -> None:
    """An item renamed where it is: its place in its list is kept under its new name."""
    row = await session.get(MenuOrder, (user_id, menu, scope))
    if row is not None and key in row.keys:
        row.keys = [to if k == key else k for k in row.keys if k != to]


async def drop_key(session: AsyncSession, user_id: str, menu: str, scope: str, key: str) -> None:
    """An item gone from a list (deleted, or moved elsewhere)."""
    row = await session.get(MenuOrder, (user_id, menu, scope))
    if row is not None and key in row.keys:
        row.keys = [k for k in row.keys if k != key]


def name_of(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def parent_scope(workspace_id: str, path: str) -> str:
    """The ``workspaces`` scope a path's item is listed in: its workspace's, or its folder's."""
    return f"{workspace_id}/{path.rsplit('/', 1)[0]}" if "/" in path else workspace_id


