"""Pages: rich HTML made from a workspace document by the person's AI app (2026-11-17, the
person's call; migration 0077).

Wysebee Assistant's ``write_page`` (apps/cloud/workspace_assistant.py) takes a whole,
self-contained HTML page for **the document open in the person's editor** and keeps it — one
page per document, its id the same each time the AI app writes it again — in the workspace's
hidden ``.pages`` folder in the workspace store (``<user>/<workspace id>/.pages/<page id>.html``,
apps/cloud/workspace_storage.py), with a :class:`~apps.cloud.models.WorkspacePage` saying which
document it is of. The folder is not one of the workspace's files: its tree and file count
leave it out (apps/cloud/workspaces.py), and deleting a document deletes its page.

A page is **what the AI app wrote, so it is shown sandboxed**. It is served from its own
address, ``GET /page-view/{page_id}/{view_key}`` — the key random and new each time it is
written, so the address is the permission (an iframe cannot carry the sign-in) — under its own
Content-Security-Policy, :data:`VIEW_CSP`: ``sandbox allow-scripts`` gives it no origin (not
Wysebee's, whose pages and sign-in it cannot reach), and it may run only its own inline styles
and scripts and reach no network — no fetch, no images or fonts from elsewhere, no forms, no
frames — so it cannot send what it shows anywhere. (Shown in ``srcdoc`` instead, it would take
the web app's own policy, which refuses inline scripts.) The website frames it with
``sandbox="allow-scripts"`` too.

**Made directly** (2026-11-27, the person's call; migration 0085): a ``.html`` file in the
workspace is a page of its own — ``kind="file"``, its bytes the file itself, kept by
:func:`keep_file` each time the file is written (by the person, or by the AI app's
``write_document`` once they save it), its title the file's ``<title>``. ``write_page`` refuses
such a file: it is written as any document is. A page made from a document is ``kind="view"``.
Either is shown, themed and shared the same way.

**Its look follows Wysebee's** (2026-11-19, the person's call): asked for with ``?theme=dark`` or
``?theme=light``, the page is served with :data:`THEME_SCRIPT` added before ``</head>`` — the
stored page is not changed. A framed page's ``prefers-color-scheme`` is the browser's, not its
frame's, so the script turns the page's own ``@media (prefers-color-scheme: …)`` rules on or off
to match, sets ``data-theme`` and ``color-scheme`` on its ``<html>``, and takes a new look from
the window that frames it (``postMessage({wysebeeTheme})``) without reloading. With no theme the
page is served as written and follows the browser.

- ``GET /pages/{page_id}`` — ``{id, workspace_id, workspace_name, source_path, title,
  view_url, updated_at, share, share_refusal}``: the page's own address, ``#/pages/<id>`` on
  the website; ``share_refusal`` is ``{detail, code, ...}`` when the person may not share it
  publicly (below), else null.
- ``GET /workspaces/{workspace_id}/page?path=`` — ``{page: {id, title, updated_at} | null}``:
  the document's page, if it has one (the editor's Page view).
- ``DELETE /pages/{page_id}`` — 204.
- ``PUT /pages/{page_id}/share`` ``{audience: "public"}`` — the page's :func:`share_of`; ``DELETE``
  it — 204, shared no more.

Another person's page is a 404.

**Shared with everyone** (2026-11-25, the person's call; migration 0084): its owner can share a
page publicly — anyone with its public address, ``/s/<share key>`` at the site's root
(:data:`share_router`), sees it with no sign-in. The key is random, made when it is shared and
kept when the AI app writes the page again, so the address keeps showing the page as it is now;
stopping sharing drops it, and sharing again makes a new one, so an address handed out before
stops working. **Whether the person may** is the policy's ``may_share``
(apps/cloud/extensions.py; on the hosted service, not on the Free plan): refused, sharing is
answered with the refusal, and a page shared before is a 404 at its address until they may again.
It is served as :func:`view_page` serves it — :data:`PUBLIC_CSP`: sandboxed, no
network, and framed by nobody — and asks search engines not to list it. ``audience`` is one of
:data:`AUDIENCES`: only ``"public"`` for now (friends and colleagues to come).

**On a domain of their own** (2026-11-25, the person's call): with ``share_url`` set
(``WYSEBEE_SHARE_URL``, e.g. ``https://wysebee.io``) the public address is there, never on this
site, and :func:`share_host_gate` makes that domain answer with shared pages only — ``/s/<key>``,
a ``robots.txt`` that lists nothing, and its root sent on to this site; everything else there
is a 404, so Wysebee's app, API and sign-in are never served under it. What people make then
runs apart from Wysebee's origin and cookies even if its sandbox failed, and a page reported as
phishing gets that domain flagged, not Wysebee's.
"""

import hmac
import html
import logging
import re
import secrets
import uuid
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.cloud.extensions import Denied, Refusal, require_access
from apps.cloud.auth import User, current_user
from apps.cloud.deps import get_session
from apps.cloud.models import PageToolGrant, UserWorkspace, WorkspacePage
from apps.cloud.page_tools import BRIDGE_SCRIPT, STUB_SCRIPT, with_script
from apps.cloud.workspace_storage import WorkspaceStore

logger = logging.getLogger("wysebee.cloud.workspace_pages")

router = APIRouter(prefix="/v1", tags=["Workspaces"])
# A shared page's public address, at the site's root (apps/cloud/main.py, before the SPA's catch-all).
share_router = APIRouter(include_in_schema=False)

FOLDER = ".pages"
MAX_BYTES = 2_000_000
MAX_TITLE = 200
# What a page may do where it is shown: no origin of its own, its own inline styles and scripts,
# pictures, fonts and media only as data, no network, no forms or frames, framed only by Wysebee.
VIEW_CSP = "; ".join([
    "sandbox allow-scripts",
    "default-src 'none'",
    "style-src 'unsafe-inline'",
    "script-src 'unsafe-inline'",
    "img-src data: blob:",
    "font-src data:",
    "media-src data: blob:",
    "connect-src 'none'",
    "frame-src 'none'",
    "form-action 'none'",
    "base-uri 'none'",
    "frame-ancestors 'self'",
])
# A page shared publicly, opened on its own at its public address: as above, framed by nobody.
PUBLIC_CSP = VIEW_CSP.replace("frame-ancestors 'self'", "frame-ancestors 'none'")
AUDIENCES = ("public",)


THEMES = ("dark", "light")
# Added to a page asked for in a look (``__THEME__`` is one of THEMES, nothing else): its rules
# for the other look off, for this one on — each rule's own text kept, so it can switch back.
THEME_SCRIPT = """<script>(function(){var t="__THEME__",kept=new WeakMap();
var on="(min-resolution: 0dppx)",off="(max-resolution: 0dppx)";
function walk(rules){for(var i=0;i<rules.length;i++){var r=rules[i];
if(r.media&&r.cssRules){if(!kept.has(r))kept.set(r,r.media.mediaText);var was=kept.get(r);
if(/prefers-color-scheme/i.test(was)){var now=was.replace(/\\(\\s*prefers-color-scheme\\s*:\\s*(dark|light)\\s*\\)/gi,function(_,s){return s.toLowerCase()===t?on:off;});
if(r.media.mediaText!==now)r.media.mediaText=now;}}
if(r.cssRules)walk(r.cssRules);}}
function apply(){var h=document.documentElement;h.setAttribute("data-theme",t);h.style.colorScheme=t;
for(var i=0;i<document.styleSheets.length;i++){try{walk(document.styleSheets[i].cssRules);}catch(e){}}}
apply();document.addEventListener("DOMContentLoaded",apply);
addEventListener("message",function(e){var d=e.data;if(e.source!==parent||!d||(d.wysebeeTheme!=="dark"&&d.wysebeeTheme!=="light"))return;t=d.wysebeeTheme;apply();});
})();</script>"""


def themed(html: bytes, theme: str) -> bytes:
    """The page with :data:`THEME_SCRIPT` for ``theme`` before its ``</head>`` (its head's
    styles read by then), else at its start."""
    script = THEME_SCRIPT.replace("__THEME__", theme).encode()
    at = html.lower().find(b"</head>")
    return html[:at] + script + html[at:] if at >= 0 else script + html


def key(workspace_id: str, page_id: str) -> str:
    return f"{workspace_id}/{FOLDER}/{page_id}.html"


def is_html(path: str) -> bool:
    """A workspace file that is a page made directly (``kind="file"``)."""
    return path.lower().endswith(".html")


def _where(page: WorkspacePage) -> str:
    """Where the page's bytes are in the workspace store: the ``.html`` file itself, or ``.pages``."""
    return f"{page.workspace_id}/{page.source_path}" if page.kind == "file" else key(page.workspace_id, page.page_id)


_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def title_of(data: bytes, path: str) -> str:
    """A page file's ``<title>``, else its file name without ``.html``."""
    found = _TITLE_RE.search(data[:65536])
    title = html.unescape(found.group(1).decode("utf-8", "replace")) if found else ""
    title = " ".join(title.split())[:MAX_TITLE]
    return title or path.rsplit("/", 1)[-1][: -len(".html")]


async def keep_file(session: AsyncSession, store: WorkspaceStore, sub: str, user_id: str, workspace_id: str, path: str, data: bytes) -> WorkspacePage:
    """A ``.html`` file written: its page made, or its title and address renewed (its id, and a
    public address, kept). A page the AI app kept for it in ``.pages`` before becomes the file
    itself. The caller commits."""
    page = await session.scalar(select(WorkspacePage).where(WorkspacePage.workspace_id == workspace_id, WorkspacePage.source_path == path))
    title, key_now = title_of(data, path), secrets.token_urlsafe(24)
    if page is None:
        page = WorkspacePage(page_id=str(uuid.uuid4()), user_id=user_id, workspace_id=workspace_id, source_path=path, title=title, view_key=key_now, kind="file")
        session.add(page)
        await session.flush()
        return page
    if page.kind != "file":
        await store.delete(sub, key(workspace_id, page.page_id))
    page.kind, page.title, page.view_key, page.updated_at = "file", title, key_now, func.now()
    return page


def is_page_path(path: str) -> bool:
    """A path in the workspace store that is a page, not one of the workspace's files."""
    return path == FOLDER or path.startswith(FOLDER + "/")


def _store(request: Request) -> WorkspaceStore:
    return request.app.state.workspace_store


async def save(session: AsyncSession, store: WorkspaceStore, sub: str, user_id: str, workspace: UserWorkspace, source_path: str, html: str, title: str) -> WorkspacePage:
    """The document's page, made or written again (its id kept): :class:`ValueError` saying
    what is wrong with it. Commits."""
    if is_html(source_path):
        raise ValueError(f"{source_path} is a page itself: write its HTML with write_document, for the person to review and save.")
    data = html.encode("utf-8")
    if not html.strip():
        raise ValueError("Give the page's whole HTML as 'html'.")
    if len(data) > MAX_BYTES:
        raise ValueError(f"A page is at most {MAX_BYTES} bytes: keep it to this document, with no pictures inlined that it does not need.")
    title = " ".join(title.split())[:MAX_TITLE]
    page = await session.scalar(select(WorkspacePage).where(WorkspacePage.workspace_id == workspace.workspace_id, WorkspacePage.source_path == source_path))
    # A new key each write: an address shown before stops working.
    key_now = secrets.token_urlsafe(24)
    if page is None:
        page = WorkspacePage(page_id=str(uuid.uuid4()), user_id=user_id, workspace_id=workspace.workspace_id, source_path=source_path, title=title, view_key=key_now)
        session.add(page)
    else:
        page.title, page.view_key, page.updated_at = title, key_now, func.now()
    await store.write(sub, key(workspace.workspace_id, page.page_id), data, "text/html; charset=utf-8")
    await session.commit()
    await session.refresh(page)
    logger.info("page_written bytes=%d", len(data))
    return page


async def forget(session: AsyncSession, store: WorkspaceStore, sub: str, workspace_id: str, source_path: str) -> None:
    """A document deleted: its page goes too (the caller commits)."""
    page = await session.scalar(select(WorkspacePage).where(WorkspacePage.workspace_id == workspace_id, WorkspacePage.source_path == source_path))
    if page is not None:
        if page.kind != "file":
            await store.delete(sub, key(workspace_id, page.page_id))
        await session.delete(page)


async def _pages_under(session: AsyncSession, workspace_id: str, folder: str) -> list[WorkspacePage]:
    under = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
    return list((await session.scalars(select(WorkspacePage).where(WorkspacePage.workspace_id == workspace_id, WorkspacePage.source_path.like(under, escape="\\")))).all())


async def forget_under(session: AsyncSession, store: WorkspaceStore, sub: str, workspace_id: str, folder: str) -> None:
    """A folder deleted: the pages of every document in it go too (the caller commits)."""
    for page in await _pages_under(session, workspace_id, folder):
        if page.kind != "file":
            await store.delete(sub, key(workspace_id, page.page_id))
        await session.delete(page)


async def move(session: AsyncSession, workspace_id: str, source_path: str, to: str, data: bytes) -> None:
    """A document renamed: its page follows it — the same page, id, view address and public
    address (``share_key``), so a link already given out keeps working (the caller commits). A
    page made directly that has no ``<title>`` takes its new file name as its title."""
    page = await session.scalar(select(WorkspacePage).where(WorkspacePage.workspace_id == workspace_id, WorkspacePage.source_path == source_path))
    if page is not None:
        page.source_path = to
        if page.kind == "file":
            page.title = title_of(data, to)


async def move_to(session: AsyncSession, store: WorkspaceStore, sub: str, workspace_id: str, path: str, to_workspace: str, to: str) -> None:
    """A file or a folder moved, maybe to another of the person's workspaces: the pages of what
    it holds follow, keeping their ids and addresses, public links too (the caller commits). A
    page kept in ``.pages`` moves to the new workspace's; one made directly moved with its file."""
    pages = await _pages_under(session, workspace_id, path)
    page = await session.scalar(select(WorkspacePage).where(WorkspacePage.workspace_id == workspace_id, WorkspacePage.source_path == path))
    for page in ([page] if page is not None else []) + pages:
        if page.kind != "file" and to_workspace != workspace_id:
            data = await store.read(sub, key(workspace_id, page.page_id))
            if data is not None:
                await store.write(sub, key(to_workspace, page.page_id), data, "text/html; charset=utf-8")
                await store.delete(sub, key(workspace_id, page.page_id))
        page.workspace_id, page.source_path = to_workspace, to + page.source_path[len(path) :]


async def move_under(session: AsyncSession, workspace_id: str, folder: str, to: str) -> None:
    """A folder renamed: the pages of its documents follow them, keeping their ids and
    addresses (the caller commits). A page made directly moved with its file; one in ``.pages``
    is named by its id, so only the row changes."""
    for page in await _pages_under(session, workspace_id, folder):
        page.source_path = to + page.source_path[len(folder) :]


async def _mine(session: AsyncSession, user: User, page_id: str) -> WorkspacePage:
    try:
        pid = str(uuid.UUID(page_id))
    except ValueError:
        pid = ""
    page = await session.get(WorkspacePage, pid) if pid else None
    if page is None or page.user_id != user.sub:
        raise HTTPException(status_code=404, detail="There is no such page.")
    return page


def _host(request: Request) -> str:
    """The host the request was made to (as :func:`apps.cloud.relay.public_base` reads it)."""
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip() or request.headers.get("host") or request.url.netloc
    return host.lower()


def _share_host(request: Request) -> str | None:
    """``share_url``'s host, when shared pages have a domain of their own."""
    url = request.app.state.settings.share_url
    return urlsplit(url).netloc if url else None


SHARE_ROBOTS = "User-agent: *\nDisallow: /\n"


async def share_host_gate(request: Request, call_next: Any) -> Response:
    """On the shared pages' own domain, those pages only (module docstring): the app's own
    routes never answer there."""
    host = _share_host(request)
    if host is None or _host(request) != host:
        return await call_next(request)
    path = request.url.path
    if path.startswith("/s/"):
        return await call_next(request)
    if path == "/robots.txt":
        return Response(SHARE_ROBOTS, media_type="text/plain")
    public_url = request.app.state.settings.public_url
    if path == "/" and public_url:
        return RedirectResponse(public_url, status_code=308)
    return Response("Not Found", status_code=404, media_type="text/plain")


def share_of(request: Request, page: WorkspacePage) -> dict[str, Any] | None:
    """Who the page is shared with, and its address there: ``{audience, url, shared_at}``, or
    None when only its owner sees it. On ``share_url`` when it is set, else on this site."""
    from apps.cloud.relay import public_base

    if not page.share_audience or not page.share_key:
        return None
    base = request.app.state.settings.share_url or public_base(request)
    return {"audience": page.share_audience, "url": f"{base}/s/{page.share_key}", "shared_at": page.shared_at}


async def _share_refusal(request: Request, session: AsyncSession, user_id: str) -> Refusal | None:
    """Why ``user_id`` may not share a page publicly (the policy's ``may_share``), or None."""
    return await request.app.state.policy.may_share(session, user_id)


@router.get("/pages/{page_id}", dependencies=[Depends(require_access)])
async def get_page(page_id: str, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    page = await _mine(session, user, page_id)
    workspace = await session.get_one(UserWorkspace, page.workspace_id)
    return {
        "id": page.page_id, "workspace_id": page.workspace_id, "workspace_name": workspace.name, "source_path": page.source_path,
        "kind": page.kind, "title": page.title, "view_url": f"/api/v1/page-view/{page.page_id}/{page.view_key}", "updated_at": page.updated_at,
        "share": share_of(request, page),
        # Tools it may run without asking (apps/cloud/page_tools.py): sharing it says they will not run there.
        "tool_grants": await session.scalar(select(func.count()).select_from(PageToolGrant).where(PageToolGrant.page_id == page.page_id)),
        "share_refusal": None if (r := await _share_refusal(request, session, user.sub)) is None else {"detail": r.detail, "code": r.code, **r.more},
    }


@router.put("/pages/{page_id}/share", dependencies=[Depends(require_access)])
async def share_page(page_id: str, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Share the page with ``audience`` (:data:`AUDIENCES`). Already shared: its address kept."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    audience = body.get("audience") if isinstance(body, dict) else None
    if audience not in AUDIENCES:
        raise HTTPException(status_code=422, detail="Say who to share it with: 'audience' is \"public\".")
    page = await _mine(session, user, page_id)
    refusal = await _share_refusal(request, session, user.sub)
    if refusal is not None:
        raise Denied(refusal)
    if page.share_key is None:
        page.share_key, page.shared_at = secrets.token_urlsafe(24), func.now()
    page.share_audience = audience
    await session.commit()
    await session.refresh(page)
    logger.info("page_shared audience=%s", audience)
    return share_of(request, page) or {}


@router.delete("/pages/{page_id}/share", status_code=204, dependencies=[Depends(require_access)])
async def unshare_page(page_id: str, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> Response:
    """Only its owner sees it again: its public address stops working."""
    page = await _mine(session, user, page_id)
    page.share_audience = page.share_key = page.shared_at = None
    await session.commit()
    return Response(status_code=204)


@share_router.get("/s/{share_key}")
async def shared_page(share_key: str, request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """A page shared publicly, for anyone, signed in or not: under :data:`PUBLIC_CSP`, as written
    (it follows the browser's look), and not for search engines to list."""
    from apps.cloud.asset_storage import user_folder

    # With a domain of their own, never here (:func:`share_host_gate` lets them through there).
    elsewhere = _share_host(request) not in (None, _host(request))
    page = None if elsewhere or not 0 < len(share_key) <= 64 else await session.scalar(select(WorkspacePage).where(WorkspacePage.share_key == share_key))
    if page is None or page.share_audience != "public" or await _share_refusal(request, session, page.user_id) is not None:
        raise HTTPException(status_code=404, detail="There is no such page, or it is no longer shared.")
    data = await _store(request).read(user_folder(page.user_id), _where(page))
    if data is None:
        raise HTTPException(status_code=404, detail="There is no such page, or it is no longer shared.")
    # Nobody's tools run for a visitor: wysebee.callTool refuses (apps/cloud/page_tools.py).
    data = with_script(data, STUB_SCRIPT)
    return HTMLResponse(data, headers={
        "Content-Security-Policy": PUBLIC_CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow",
    })


@router.get("/page-view/{page_id}/{view_key}", include_in_schema=False)
async def view_page(
    page_id: str, view_key: str, request: Request, theme: str | None = Query(None), session: AsyncSession = Depends(get_session)
) -> Response:
    """The page itself, for the website's iframe: its address its permission, under
    :data:`VIEW_CSP` — in Wysebee's look when ``theme`` names one (:func:`themed`)."""
    from apps.cloud.asset_storage import user_folder

    try:
        pid = str(uuid.UUID(page_id))
    except ValueError:
        pid = ""
    page = await session.get(WorkspacePage, pid) if pid else None
    if page is None or not hmac.compare_digest(page.view_key.encode(), view_key.encode()):
        raise HTTPException(status_code=404, detail="There is no such page.")
    data = await _store(request).read(user_folder(page.user_id), _where(page))
    if data is None:
        gone = "The page's file is gone from the workspace." if page.kind == "file" else "The page's file is gone: ask your AI app to write it again."
        raise HTTPException(status_code=404, detail=gone)
    if theme in THEMES:
        data = themed(data, theme)
    # Shown to its owner in Wysebee: wysebee.callTool asks the web app framing it (apps/cloud/page_tools.py).
    data = with_script(data, BRIDGE_SCRIPT)
    return HTMLResponse(data, headers={
        "Content-Security-Policy": VIEW_CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
    })


@router.get("/workspaces/{workspace_id}/page", dependencies=[Depends(require_access)])
async def document_page(
    workspace_id: str, request: Request, path: str = Query(...), user: User = Depends(current_user), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """The document's page; a ``.html`` file written before pages could be made directly gets
    its page now."""
    from apps.cloud.workspaces import _sub, _workspace, valid_path

    workspace = await _workspace(session, user, workspace_id)
    path = valid_path(path)
    page = await session.scalar(select(WorkspacePage).where(WorkspacePage.workspace_id == workspace.workspace_id, WorkspacePage.source_path == path))
    if page is None and is_html(path):
        store, sub = _store(request), _sub(user)
        data = await store.read(sub, f"{workspace.workspace_id}/{path}")
        if data is not None:
            page = await keep_file(session, store, sub, user.sub, workspace.workspace_id, path, data)
            await session.commit()
            await session.refresh(page)
    return {"page": None if page is None else {"id": page.page_id, "title": page.title, "updated_at": page.updated_at}}


@router.delete("/pages/{page_id}", status_code=204, dependencies=[Depends(require_access)])
async def delete_page(page_id: str, request: Request, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)) -> Response:
    from apps.cloud.workspaces import _sub

    page = await _mine(session, user, page_id)
    if page.kind == "file":
        raise HTTPException(status_code=409, detail=f"This page is the file {page.source_path}: delete the file instead.")
    await _store(request).delete(_sub(user), key(page.workspace_id, page.page_id))
    await session.delete(page)
    await session.commit()
    return Response(status_code=204)
