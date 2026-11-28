"""Workspaces: a person's documentation, a folder of text files each (2026-11-14).

A workspace is a row of ``user_workspaces`` — its id (a UUID), its name and its ``resource_url``
— and a folder in the workspace store (apps/cloud/workspace_storage.py): ``user-workspaces`` in
production, provisioned per person by their user id as skill files are, ``<user id>/<workspace
id>/<path>``. Everything links by id, so a workspace can be renamed and nothing moves. (Until migration
0073 workspaces were called projects; those were dropped.) **The folder structure is the store's own**: a workspace's tree is read by
listing its folder, and a file is named by its path. Creating a workspace writes its first
file, ``README.md``, so the folder exists from the start; an empty folder holds an empty
``.keep``. The routes:

- ``GET /workspaces`` — ``{workspaces: [workspace]}``, by name; a workspace is ``{id, name,
  resource_url, created_at, updated_at}``.
- ``POST /workspaces`` ``{name}`` — 201 the new workspace. A name is 1–80 characters of
  letters, digits, spaces, ``-``, ``_`` and ``.``, starting with a letter or digit and not
  ending in a space or a dot; one the person already has, whatever its case, is a 409; past
  ``WYSEBEE_WORKSPACE_MAX_COUNT`` a 409.
- ``GET /workspaces/{id}`` — the workspace with ``files: [{path, size, updated_at}]`` and
  ``folders: [path]``, each sorted. ``PUT /workspaces/{id}`` ``{name}`` — renames it (the
  name's rules and 409 as for a new one). An id is taken with or without its dashes.
- ``DELETE /workspaces/{id}`` — 204; the row first, then its folder (best effort).
- ``GET /workspaces/{id}/files/{path}`` — ``{path, content, size, updated_at}``; a file is
  UTF-8 text.
- ``PUT /workspaces/{id}/files/{path}`` ``{content}`` — writes it, its folders made with it;
  ``{path, size}``. At most ``WYSEBEE_WORKSPACE_FILE_MAX_BYTES`` (413); a new file past
  ``WYSEBEE_WORKSPACE_MAX_FILES`` is a 409.
- ``DELETE /workspaces/{id}/files/{path}`` — 204.
- ``PATCH /workspaces/{id}/files/{path}`` ``{name}`` — renames it where it is (2026-11-28);
  ``{path}`` the new one. Its page goes with it, the same page at the same addresses, its
  public link too. A name taken there (a file or a folder, whatever its case) is a 409; a
  page (``.html``) stays a page and a document stays a document (422).
- ``POST /workspaces/{id}/folders/{path}`` — 201 ``{path}``; ``DELETE`` — 204, with all it holds.
- ``PATCH /workspaces/{id}/folders/{path}`` ``{name}`` — renames it where it is (2026-11-28):
  everything in it moves to ``{path}`` with the new last segment, its pages with their
  documents; ``{path}`` the new one. A name taken there (a file or a folder) is a 409.

Pages the person's AI app made from its documents are kept in its hidden ``.pages`` folder
(apps/cloud/workspace_pages.py): never among its files, and deleted with their document (or
its folder). A ``.html`` file is a page made directly (2026-11-27): written, it is shown as a
page, sandboxed, at its own address.

A path is up to ten segments of the same characters as a name (no hidden, ``.`` or ``..``),
400 characters at most; anything else is a 422. Another person's workspace is a 404.
"""

import logging
import re
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from apps.cloud.asset_storage import user_folder
from apps.cloud.auth import User, current_user
from apps.cloud.asset_storage import InvalidName
from apps.cloud.config import Settings
from apps.cloud.deps import get_session
from apps.cloud.models import UserWorkspace
from apps.cloud.people import Person, named, of_rows, stamp
from apps.cloud import workspace_pages
from apps.cloud.workspace_storage import FOLDER_MARKER, AzureBlobWorkspaceStore, FileWorkspaceStore, WorkspaceStore

logger = logging.getLogger("wysebee.cloud.workspaces")

router = APIRouter(prefix="/v1", tags=["Workspaces"])

# A name, and each segment of a path: no slash, nothing hidden, no trailing space or dot.
SEGMENT_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9 ._-]*[A-Za-z0-9_-])?$")
NAME_MAX = 80
SEGMENT_MAX = 100
PATH_MAX = 400
PATH_DEPTH = 10
README = "README.md"
# The per-person advisory lock a create takes (skill files use 7_274_101).
_LOCK_CLASS = 7_274_102

WorkspaceId = Annotated[str, Path(pattern=r"^[0-9a-fA-F-]{32,36}$")]


class NewWorkspace(BaseModel):
    name: str


class NewName(BaseModel):
    name: str


class FileBody(BaseModel):
    content: str


def get_workspace_store(request: Request) -> WorkspaceStore:
    return request.app.state.workspace_store


def make_workspace_store(settings: Settings) -> WorkspaceStore:
    """The asset store's kind and account, a container (or folder) of their own."""
    if settings.asset_store == "azure":
        if not settings.asset_account_url:
            raise RuntimeError("WYSEBEE_ASSET_STORE=azure needs WYSEBEE_ASSET_AZURE_ACCOUNT_URL (or WYSEBEE_BACKUP_AZURE_ACCOUNT_URL).")
        return AzureBlobWorkspaceStore.from_account_url(settings.asset_account_url, settings.workspace_azure_container)
    return FileWorkspaceStore(settings.workspace_dir)


def valid_name(name: str) -> str:
    """The workspace's name, or a 422 saying what one may be."""
    name = name.strip()
    if not (0 < len(name) <= NAME_MAX and SEGMENT_RE.match(name)):
        raise HTTPException(
            status_code=422,
            detail=f"A workspace's name is 1 to {NAME_MAX} letters, digits, spaces, '-', '_' or '.', starting with a letter or digit.",
        )
    return name


def valid_path(path: str) -> str:
    """A file's or folder's path inside a workspace, or a 422."""
    parts = path.strip("/").split("/")
    if (
        not path.strip("/")
        or len(path) > PATH_MAX
        or len(parts) > PATH_DEPTH
        or not all(len(p) <= SEGMENT_MAX and SEGMENT_RE.match(p) for p in parts)
    ):
        raise HTTPException(
            status_code=422,
            detail="A path is up to ten folders and a name, each of letters, digits, spaces, '-', '_' or '.', starting with a letter or digit.",
        )
    return "/".join(parts)


def content_type(path: str) -> str:
    return "text/markdown; charset=utf-8" if path.lower().endswith((".md", ".markdown")) else "text/plain; charset=utf-8"


def out(p: UserWorkspace, who: dict[str, Person]) -> dict[str, Any]:
    return {
        "id": p.workspace_id, "name": p.name, "resource_url": p.resource_url, "created_at": p.created_at, "updated_at": p.updated_at,
        # Who made it and who changed it (or a file of it) last (apps/cloud/people.py).
        "created_by": named(who, p.created_by).model_dump(), "updated_by": named(who, p.updated_by).model_dump(),
    }


async def answer(session: AsyncSession, workspace: UserWorkspace) -> dict[str, Any]:
    return out(workspace, await of_rows(session, [workspace]))


def _not_found(what: str = "Workspace") -> HTTPException:
    return HTTPException(status_code=404, detail=f"{what} not found.")


def _sub(user: User) -> str:
    try:
        return user_folder(user.sub)
    except InvalidName as exc:
        raise HTTPException(status_code=422, detail="This account's id cannot hold files.") from exc


async def _workspace(session: AsyncSession, user: User, workspace_id: str) -> UserWorkspace:
    """The person's workspace, or a 404."""
    try:
        workspace_id = str(uuid.UUID(workspace_id))
    except ValueError:
        raise _not_found() from None
    workspace = await session.scalar(select(UserWorkspace).where(UserWorkspace.workspace_id == workspace_id))
    if workspace is None or workspace.user_id != user.sub:
        raise _not_found()
    return workspace


async def _name_free(session: AsyncSession, user: User, name: str, but: str | None = None) -> None:
    """A 409 when the person has another workspace of that name, whatever its case — after the
    per-person lock, so two writes cannot both take it."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:c, hashtext(:sub))"), {"c": _LOCK_CLASS, "sub": user.sub})
    query = select(UserWorkspace.workspace_id).where(UserWorkspace.user_id == user.sub, func.lower(UserWorkspace.name) == name.lower())
    taken = await session.scalar(query.where(UserWorkspace.workspace_id != but) if but else query)
    if taken is not None:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"You already have a workspace named {name!r}.")


def _no_file_above(path: str, files: list[str]) -> None:
    """A 409 when a file sits where one of the path's folders would."""
    clash = next((f for f in files if path.startswith(f + "/")), None)
    if clash is not None:
        raise HTTPException(status_code=409, detail=f"{clash!r} is a file.")


async def _touch(session: AsyncSession, workspace: UserWorkspace, user: User) -> None:
    workspace.updated_at = func.now()
    stamp(workspace, user.sub)
    await session.commit()


@router.get("/workspaces")
async def list_workspaces(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    rows = (await session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == user.sub).order_by(func.lower(UserWorkspace.name)))).all()
    who = await of_rows(session, rows)
    return {"workspaces": [out(p, who) for p in rows]}


@router.post("/workspaces", status_code=201)
async def create_workspace(
    body: NewWorkspace,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    store: WorkspaceStore = Depends(get_workspace_store),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    sub = _sub(user)
    name = valid_name(body.name)
    await _name_free(session, user, name)
    count = await session.scalar(select(func.count()).select_from(UserWorkspace).where(UserWorkspace.user_id == user.sub))
    if count >= settings.workspace_max_count:
        await session.rollback()
        raise HTTPException(status_code=409, detail=f"You have {count} workspaces, the most allowed is {settings.workspace_max_count}. Delete one first.")
    # The folder first, so a row never names a folder that is not there.
    workspace_id = str(uuid.uuid4())
    starter = f"# {name}\n\nStart writing {name}'s documentation here.\n".encode()
    await store.write(sub, f"{workspace_id}/{README}", starter, content_type(README))
    workspace = UserWorkspace(workspace_id=workspace_id, user_id=user.sub, name=name, resource_url=store.url(sub, workspace_id))
    stamp(workspace, user.sub, made=True)
    session.add(workspace)
    await session.commit()
    await session.refresh(workspace)
    return await answer(session, workspace)


@router.get("/workspaces/{workspace_id}")
async def get_workspace(
    workspace_id: WorkspaceId,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    store: WorkspaceStore = Depends(get_workspace_store),
) -> dict[str, Any]:
    workspace = await _workspace(session, user, workspace_id)
    # Its pages (apps/cloud/workspace_pages.py) are not among its files.
    found = [f for f in await store.list(_sub(user), workspace.workspace_id) if not workspace_pages.is_page_path(f.path)]
    files = [{"path": f.path, "size": f.size, "updated_at": f.updated_at} for f in found if f.path.rsplit("/", 1)[-1] != FOLDER_MARKER]
    folders = sorted({"/".join(f.path.split("/")[:i]) for f in found for i in range(1, f.path.count("/") + 1)})
    return {**await answer(session, workspace), "files": files, "folders": folders}


@router.put("/workspaces/{workspace_id}")
async def rename_workspace(
    workspace_id: WorkspaceId,
    body: NewWorkspace,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Rename a workspace: its files stay where they are."""
    name = valid_name(body.name)
    workspace = await _workspace(session, user, workspace_id)
    await _name_free(session, user, name, but=workspace.workspace_id)
    workspace = await _workspace(session, user, workspace_id)
    workspace.name, workspace.updated_at = name, func.now()
    stamp(workspace, user.sub)
    await session.commit()
    await session.refresh(workspace)
    return await answer(session, workspace)


@router.delete("/workspaces/{workspace_id}", status_code=204)
async def delete_workspace(
    workspace_id: WorkspaceId,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    store: WorkspaceStore = Depends(get_workspace_store),
) -> Response:
    workspace = await _workspace(session, user, workspace_id)
    folder = workspace.workspace_id
    await session.delete(workspace)
    await session.commit()
    try:
        await store.delete_prefix(_sub(user), folder)
    except Exception as exc:  # noqa: BLE001 — stray files are harmless; the answer stands
        logger.warning("workspace %s deleted but its files were not: %s", workspace_id, exc)
    return Response(status_code=204)


@router.get("/workspaces/{workspace_id}/files/{file_path:path}")
async def read_file(
    workspace_id: WorkspaceId,
    file_path: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    store: WorkspaceStore = Depends(get_workspace_store),
) -> dict[str, Any]:
    workspace = await _workspace(session, user, workspace_id)
    path = valid_path(file_path)
    await session.close()
    data = await store.read(_sub(user), f"{workspace.workspace_id}/{path}")
    if data is None:
        raise _not_found("File")
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="This file is not text.") from exc
    return {"path": path, "content": content, "size": len(data)}


@router.put("/workspaces/{workspace_id}/files/{file_path:path}")
async def write_file(
    workspace_id: WorkspaceId,
    file_path: str,
    body: FileBody,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    store: WorkspaceStore = Depends(get_workspace_store),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    workspace = await _workspace(session, user, workspace_id)
    path = valid_path(file_path)
    data = body.content.encode("utf-8")
    if len(data) > settings.workspace_file_max_bytes:
        raise HTTPException(status_code=413, detail=f"A workspace file is at most {settings.workspace_file_max_bytes} bytes.")
    sub = _sub(user)
    folder = workspace.workspace_id
    present = [f for f in await store.list(sub, folder) if not workspace_pages.is_page_path(f.path)]
    files = [f.path for f in present if f.path.rsplit("/", 1)[-1] != FOLDER_MARKER]
    if path not in files and len(files) >= settings.workspace_max_files:
        raise HTTPException(status_code=409, detail=f"A workspace holds at most {settings.workspace_max_files} files.")
    if any(f.path.startswith(path + "/") for f in present):
        raise HTTPException(status_code=409, detail=f"{path!r} is a folder.")
    _no_file_above(path, files)
    await store.write(sub, f"{folder}/{path}", data, content_type(path))
    if workspace_pages.is_html(path):
        # A page made directly: its page row says how it is shown (apps/cloud/workspace_pages.py).
        await workspace_pages.keep_file(session, store, sub, user.sub, folder, path, data)
    await _touch(session, workspace, user)
    return {"path": path, "size": len(data)}


@router.delete("/workspaces/{workspace_id}/files/{file_path:path}", status_code=204)
async def delete_file(
    workspace_id: WorkspaceId,
    file_path: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    store: WorkspaceStore = Depends(get_workspace_store),
) -> Response:
    workspace = await _workspace(session, user, workspace_id)
    path = valid_path(file_path)
    sub = _sub(user)
    folder = workspace.workspace_id
    if await store.read(sub, f"{folder}/{path}") is None:
        raise _not_found("File")
    # Its folder stays when it was the last thing in it.
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    await store.delete(sub, f"{folder}/{path}")
    # Its page (if the AI app made one) goes with it.
    await workspace_pages.forget(session, store, sub, folder, path)
    if parent and not await store.list(sub, f"{folder}/{parent}"):
        await store.write(sub, f"{folder}/{parent}/{FOLDER_MARKER}", b"", "text/plain")
    await _touch(session, workspace, user)
    return Response(status_code=204)


@router.patch("/workspaces/{workspace_id}/files/{file_path:path}")
async def rename_file(
    workspace_id: WorkspaceId,
    file_path: str,
    body: NewName,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    store: WorkspaceStore = Depends(get_workspace_store),
) -> dict[str, Any]:
    """Rename a file where it is; its page (and any link to it) stays the same."""
    workspace = await _workspace(session, user, workspace_id)
    path = valid_path(file_path)
    name = body.name.strip()
    if "/" in name:
        raise HTTPException(status_code=422, detail="A file's name has no '/' in it.")
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    new = valid_path(f"{parent}/{name}" if parent else name)
    if workspace_pages.is_html(new) != workspace_pages.is_html(path):
        raise HTTPException(status_code=422, detail="A page's name ends in .html, a document's does not: keep it what it is.")
    sub = _sub(user)
    folder = workspace.workspace_id
    data = await store.read(sub, f"{folder}/{path}")
    if data is None:
        raise _not_found("File")
    if new == path:
        return {"path": path}
    # Taken whatever its case: a store that ignores case would make the two one file.
    taken = new.lower()
    present = [f.path for f in await store.list(sub, folder) if not workspace_pages.is_page_path(f.path) and f.path != path]
    if any(p.lower() == taken or p.lower().startswith(taken + "/") for p in present):
        raise HTTPException(status_code=409, detail=f"{new!r} is already there.")
    # A change of case alone is the same file on a store that ignores case: the old one goes
    # first there, or deleting it would delete the new one.
    if new.lower() == path.lower():
        await store.delete(sub, f"{folder}/{path}")
        await store.write(sub, f"{folder}/{new}", data, content_type(new))
    else:
        await store.write(sub, f"{folder}/{new}", data, content_type(new))
        await store.delete(sub, f"{folder}/{path}")
    await workspace_pages.move(session, folder, path, new, data)
    await _touch(session, workspace, user)
    return {"path": new}


@router.post("/workspaces/{workspace_id}/folders/{folder_path:path}", status_code=201)
async def make_folder(
    workspace_id: WorkspaceId,
    folder_path: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    store: WorkspaceStore = Depends(get_workspace_store),
) -> dict[str, Any]:
    workspace = await _workspace(session, user, workspace_id)
    path = valid_path(folder_path)
    sub = _sub(user)
    folder = workspace.workspace_id
    present = await store.list(sub, folder)
    _no_file_above(path + "/", [f.path for f in present])
    if not any(f.path.startswith(path + "/") for f in present):
        await store.write(sub, f"{folder}/{path}/{FOLDER_MARKER}", b"", "text/plain")
    await _touch(session, workspace, user)
    return {"path": path}


@router.delete("/workspaces/{workspace_id}/folders/{folder_path:path}", status_code=204)
async def delete_folder(
    workspace_id: WorkspaceId,
    folder_path: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    store: WorkspaceStore = Depends(get_workspace_store),
) -> Response:
    workspace = await _workspace(session, user, workspace_id)
    path = valid_path(folder_path)
    sub = _sub(user)
    folder = workspace.workspace_id
    if not await store.list(sub, f"{folder}/{path}"):
        raise _not_found("Folder")
    await store.delete_prefix(sub, f"{folder}/{path}")
    await workspace_pages.forget_under(session, store, sub, folder, path)
    await _touch(session, workspace, user)
    return Response(status_code=204)


async def _move(store: WorkspaceStore, sub: str, folder: str, src: str, dst: str, paths: list[str]) -> list[str]:
    """Copy each of ``paths`` (all under ``src``) to the same place under ``dst``, then drop
    ``src``; the paths where they are now."""
    for old in paths:
        data = await store.read(sub, f"{folder}/{old}")
        if data is not None:
            to = dst + old[len(src) :]
            await store.write(sub, f"{folder}/{to}", data, content_type(to))
    await store.delete_prefix(sub, f"{folder}/{src}")
    return [dst + p[len(src) :] for p in paths]


@router.patch("/workspaces/{workspace_id}/folders/{folder_path:path}")
async def rename_folder(
    workspace_id: WorkspaceId,
    folder_path: str,
    body: NewName,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    store: WorkspaceStore = Depends(get_workspace_store),
) -> dict[str, Any]:
    """Rename a folder where it is: what it holds moves under the new name, pages and all."""
    workspace = await _workspace(session, user, workspace_id)
    path = valid_path(folder_path)
    name = body.name.strip()
    if "/" in name:
        raise HTTPException(status_code=422, detail="A folder's name has no '/' in it.")
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    new = valid_path(f"{parent}/{name}" if parent else name)
    sub = _sub(user)
    folder = workspace.workspace_id
    present = [f for f in await store.list(sub, folder) if not workspace_pages.is_page_path(f.path)]
    inside = [f.path for f in present if f.path.startswith(path + "/")]
    if not inside:
        raise _not_found("Folder")
    if new == path:
        return {"path": path}
    # Taken whatever its case: a store that ignores case would make the two one folder.
    taken = new.lower()
    if any(f.path.lower() == taken or f.path.lower().startswith(taken + "/") for f in present if not f.path.startswith(path + "/")):
        raise HTTPException(status_code=409, detail=f"{new!r} is already there.")
    # A change of case alone names the same folder on a store that ignores case (a Mac's
    # disk): it goes by a name of its own first, so the new name is a folder of its own.
    src = path
    if new.lower() == path.lower():
        step = f"{path}.renaming-{uuid.uuid4().hex[:8]}"
        inside = await _move(store, sub, folder, path, step, inside)
        src = step
    await _move(store, sub, folder, src, new, inside)
    await workspace_pages.move_under(session, folder, path, new)
    await _touch(session, workspace, user)
    return {"path": new}
