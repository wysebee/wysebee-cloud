"""The Wysebee Cloud FastAPI app.

Serves a small JSON API under ``/api`` and the built React SPA at the site root.
Unknown non-API paths fall back to ``index.html`` so client-side routes work.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import httpx

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from apps.cloud import __version__
from apps.cloud.agent_chat.postgres import RateLimit
from apps.cloud.account_deletion import router as account_deletion_router
from apps.cloud.agent_mcp import mcp_router
from apps.cloud.agents import router as agents_router
from apps.cloud.agent_mcp import router as agent_mcp_router
from apps.cloud.tool_calls import router as tool_calls_router
from apps.cloud import agent_packs, extensions, packs
from apps.cloud import agent_types, browser_links, browser_recordings, usage_events
from apps.cloud.assets import make_asset_store
from apps.cloud.assets import router as assets_router
from apps.cloud.auth import TokenVerifier, User, current_user
from apps.cloud.config import Settings, get_settings
from apps.cloud.config_events import ChangeHub
from apps.cloud.config_events import router as config_events_router
from apps.cloud.tasks import router as tasks_router
from apps.cloud.db import Database, raise_if_cancelled
from apps.cloud.envelope import make_kek
from apps.cloud.notifications import router as notifications_router
from apps.cloud import menu_orders, page_tools, skill_changes, task_runner, workspace_pages
from apps.cloud.mock_auth import MockAuth, localhost_cors
from apps.cloud.mock_auth import router as mock_auth_router
from apps.cloud.relay import RelayHub
from apps.cloud.workspace_assistant import router as workspace_assistant_router
from apps.cloud.workspaces import make_workspace_store
from apps.cloud.workspaces import router as workspaces_router
from apps.cloud.skill_files import make_skill_file_store
from apps.cloud.skill_files import router as skill_files_router
from apps.cloud.skill_folders import router as skill_folders_router
from apps.cloud.skills import router as skills_router
from apps.cloud.user_tools import router as user_tools_router
from apps.cloud.storage import router as storage_router
from apps.cloud.userdata import router as userdata_router
from apps.cloud.users import router as users_router
from apps.cloud.web import router as web_router

logger = logging.getLogger("wysebee.cloud")

# apps/cloud/main.py -> [2] the project root (/app in the image), where alembic.ini is.
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

api = APIRouter(prefix="/api")


@api.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up. Deliberately does not touch the database, so a
    database outage does not get the container restarted."""
    return {"status": "ok"}


@api.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness: the database answers."""
    db: Database = request.app.state.db
    try:
        await db.ping()
    except Exception as exc:  # noqa: BLE001 — any failure means "not ready"
        logger.warning("database ping failed: %s", exc)
        return JSONResponse({"status": "unavailable", "database": "down"}, status_code=503)
    return JSONResponse({"status": "ok", "database": "up"})


@lru_cache
def migration_head() -> str | None:
    """The newest migration this build ships, or None if the scripts are not here."""
    if not ALEMBIC_INI.exists():
        return None
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()


@api.get("/healthcheck")
async def healthcheck(request: Request) -> JSONResponse:
    """Everything at once, for a person or a monitor: version, uptime, whether the
    database answers (how fast, or the type of error — never its message, which names
    hosts) and whether its schema is at this build's migration head.

    503 when the database is down; a schema behind the head is "degraded" but 200,
    since the app still serves what it can.
    """
    db: Database = request.app.state.db
    database: dict[str, object] = {}
    started = time.perf_counter()
    try:
        current = await db.migration_revision()
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        error = getattr(exc, "orig", None) or exc
        logger.warning("healthcheck: database unreachable: %s", exc)
        database = {"status": "down", "error": type(error).__name__}
    else:
        head = migration_head()
        database = {
            "status": "up",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "migration": {"current": current, "head": head, "up_to_date": head is None or current == head},
        }

    if database["status"] == "down":
        status, code = "down", 503
    elif not database["migration"]["up_to_date"]:  # type: ignore[index]
        status, code = "degraded", 200
    else:
        status, code = "ok", 200
    return JSONResponse(
        {
            "status": status,
            "version": __version__,
            "uptime_seconds": round(time.monotonic() - request.app.state.started_at),
            "database": database,
        },
        status_code=code,
        headers={"Cache-Control": "no-store"},
    )


@api.get("/v1/info")
async def info() -> dict[str, str]:
    return {"name": "wysebee-cloud", "version": __version__, "status": "ok"}


@api.get("/v1/me")
async def me(user: User = Depends(current_user)) -> dict[str, str]:
    """The signed-in caller, from their access token — how a client checks its sign-in."""
    return {"sub": user.sub, "username": user.username, "email": user.email, "name": user.name}


# What an extension's policy may refuse a person (apps/cloud/extensions.py, ``may_use``) —
# everything of theirs but their account, their usage, and what a device calls with its own
# secret (the device link asks the policy itself). Without an extension, nothing is refused.
paid = [Depends(extensions.require_access)]
api.include_router(notifications_router, dependencies=paid)
api.include_router(task_runner.router, dependencies=paid)
api.include_router(assets_router)
api.include_router(skill_files_router, dependencies=paid)
api.include_router(agents_router, dependencies=paid)
api.include_router(skills_router, dependencies=paid)
api.include_router(skill_folders_router, dependencies=paid)
api.include_router(skill_changes.router, dependencies=paid)
# Its page view is opened by a key, not a sign-in: its own routes ask for the plan.
api.include_router(workspace_pages.router)
# Pages run the person's tools, as their owner allows (apps/cloud/page_tools.py).
api.include_router(page_tools.router)
api.include_router(workspaces_router, dependencies=paid)
api.include_router(menu_orders.router, dependencies=paid)
api.include_router(workspace_assistant_router, dependencies=paid)
api.include_router(config_events_router)
api.include_router(tasks_router, dependencies=paid)
api.include_router(web_router)
api.include_router(agent_mcp_router, dependencies=paid)
api.include_router(userdata_router, dependencies=paid)
api.include_router(users_router)
api.include_router(user_tools_router, dependencies=paid)
api.include_router(tool_calls_router)
api.include_router(storage_router)
api.include_router(browser_links.router)
api.include_router(browser_recordings.router)
api.include_router(agent_types.router)
api.include_router(usage_events.router)


def _web_app_csp(settings: Settings) -> str:
    """The web app's Content-Security-Policy: its own files, and the sign-in provider.

    Its access token is kept in the page (localStorage), so an injected script is the
    risk to shut out: scripts only from here; requests only to here, to the Auth0
    tenant (token renewal); the tenant framed only for a silent sign-in check. Usage events go
    to this server (POST /api/v1/events), which forwards them to Mixpanel, so the page needs no
    Mixpanel host (2026-11-15)."""
    from urllib.parse import urlsplit

    auth = ""
    if settings.auth_issuer:
        parts = urlsplit(settings.auth_issuer)
        auth = f" {parts.scheme}://{parts.netloc}"
    return "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        # React sets style attributes; the stylesheet is a file.
        "style-src 'self' 'unsafe-inline'",
        # Agent avatars are data: URLs.
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        f"connect-src 'self'{auth}",
        # The pages the AI app made, shown from here, sandboxed (apps/cloud/workspace_pages.py).
        f"frame-src 'self'{auth}",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
    ])


def _mount_web_app(app: FastAPI, app_dist: Path, settings: Settings) -> None:
    """The web app (``ui/app``, built by ``cd ui && yarn build``) at ``/app/``: a
    signed-in person's cloud tasks. Absent when it was not built. Registered
    before the landing page's catch-all."""
    index = app_dist / "index.html"
    if not index.exists():
        logger.info("web app not built (%s missing): /app is not served", index)
        return
    root = app_dist.resolve()
    csp = {"Content-Security-Policy": _web_app_csp(settings), "X-Content-Type-Options": "nosniff"}

    @app.get("/app", include_in_schema=False)
    async def web_app_root() -> RedirectResponse:
        return RedirectResponse("/app/", status_code=308)

    @app.get("/app/{path:path}", include_in_schema=False)
    async def web_app(path: str) -> FileResponse:
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            # Hashed bundle files never change under one name.
            cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
            return FileResponse(candidate, headers={**csp, "Cache-Control": cache})
        # Anything else is one of the app's own (hash) routes: its shell.
        return FileResponse(index, headers={**csp, "Cache-Control": "no-cache"})


def _mount_spa(app: FastAPI, ui_dist: Path) -> None:
    index = ui_dist / "index.html"
    if not index.exists():
        logger.warning("SPA not built; %s missing. Run `cd ui && yarn build`.", index)
        return

    if (ui_dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=ui_dist / "assets"), name="ui-assets")

    root = ui_dist.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status_code=404)
        # Real files in dist (favicon, logo, ...) are served as-is; anything else
        # is a client-side route and gets the SPA shell.
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


async def _purge_turns_often(db: Database, settings: Settings) -> None:
    """Drop browser calls past their keeping time every few minutes
    (``turn_purge_seconds``), with pairing codes nobody typed (apps/cloud/browser_links.py).
    Never raises: a database outage just skips a round."""
    while True:
        try:
            async with db.sessionmaker() as session:
                removed = await browser_links.purge(session, settings)
            if removed:
                logger.info("purged %d browser calls", removed)
        except Exception as exc:  # noqa: BLE001
            raise_if_cancelled(exc)
            logger.warning("turn purge skipped: %s", exc)
        await asyncio.sleep(settings.turn_purge_seconds)


async def _seed_agent_packs(app: FastAPI) -> None:
    """Add the agent type rows the packs need (apps/cloud/agent_packs.py). Never raises: a
    database not reached leaves them for the next start."""
    try:
        async with app.state.db.sessionmaker() as session:
            added = await agent_packs.seed(session, app.state.agent_packs)
        if added:
            logger.info("agent types added for packs: %s", ", ".join(added))
    except Exception as exc:  # noqa: BLE001
        raise_if_cancelled(exc)
        logger.warning("agent pack types not added: %s", exc)


def create_app(ui_dist: Path | None = None, settings: Settings | None = None, loaded: list[extensions.Extension] | None = None) -> FastAPI:
    """The app. ``loaded``: the extensions to use — by default those the settings name
    (``WYSEBEE_EXTENSIONS``)."""
    settings = settings or get_settings()
    loaded = extensions.load(settings) if loaded is None else loaded
    if settings.selfhost and not settings.selfhost_token:
        logger.warning("SELF-HOSTED WITHOUT A TOKEN: anyone who can reach this server is its owner. Keep it on this machine or set WYSEBEE_SELFHOST_TOKEN.")
    # Local development's stand-in for Auth0 (WYSEBEE_AUTH_MOCK; apps/cloud/mock_auth.py).
    mock_auth = MockAuth(settings) if settings.auth_mock else None
    if mock_auth:
        logger.warning("MOCK SIGN-IN is on (%s): anyone can sign in as anyone. Local development only.", settings.auth_issuer)
    if settings.assistant_mcp_local_user:
        logger.warning("/assistant/mcp answers WITHOUT SIGN-IN, as %s. Local development only.", settings.assistant_mcp_local_user)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings)
        app.state.db = db
        app.state.settings = settings
        app.state.started_at = time.monotonic()
        app.state.token_verifier = TokenVerifier(settings, key=mock_auth.public_key) if mock_auth else TokenVerifier(settings)
        app.state.mock_auth = mock_auth
        app.state.asset_store = make_asset_store(settings)
        app.state.skill_file_store = make_skill_file_store(settings)
        app.state.workspace_store = make_workspace_store(settings)
        app.state.recording_store = browser_recordings.make_recording_store(settings)
        app.state.change_hub = ChangeHub(settings)
        app.state.change_hub.start()
        app.state.relay_hub = RelayHub(settings)
        app.state.relay_hub.start()
        # The KEK their PostgreSQL passwords are sealed under, and the client their Snowflake
        # calls go through (apps/cloud/agent_chat/snowflake.py, which sets each call's limit).
        app.state.ai_kek = make_kek(settings)
        app.state.ai_http = httpx.AsyncClient(timeout=300)
        # Their PostgreSQL calls, at most so many a minute each (agent_chat/postgres.py).
        app.state.postgres_limit = RateLimit()
        # …and their Snowflake calls (agent_chat/snowflake.py).
        app.state.snowflake_limit = RateLimit()
        # The browser link (apps/cloud/browser_links.py): pairing starts a minute from one
        # address, and wrong pairing codes an account may type.
        app.state.browser_link_starts = RateLimit(settings.browser_link_starts_per_minute)
        # Usage events (apps/cloud/usage_events.py): batches a minute each, and the client that
        # forwards them to Mixpanel.
        app.state.event_batches = RateLimit(settings.events_batches_per_minute)
        app.state.events_http = httpx.AsyncClient(timeout=10)
        event_purger = asyncio.create_task(usage_events.purge_often(db, settings), name="purge-usage-events")
        app.state.browser_link_tries = browser_links.Tries(settings.browser_link_tries, settings.browser_link_try_window_seconds)
        turn_purger = asyncio.create_task(_purge_turns_often(db, settings), name="purge-turns")
        # Scheduled tasks: run when due (apps/cloud/task_runner.py).
        scheduler = asyncio.create_task(task_runner.scheduler(app.state), name="task-scheduler")
        await _seed_agent_packs(app)
        try:
            yield
        finally:
            for task in (turn_purger, scheduler, event_purger):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await app.state.change_hub.stop()
            await app.state.relay_hub.stop()
            await app.state.asset_store.aclose()
            await app.state.skill_file_store.aclose()
            await app.state.workspace_store.aclose()
            await app.state.recording_store.aclose()
            await app.state.ai_http.aclose()
            await app.state.events_http.aclose()
            if app.state.ai_kek is not None:
                await app.state.ai_kek.aclose()
            await db.dispose()

    app = FastAPI(title="Wysebee Cloud", version=__version__, lifespan=lifespan)
    # What extensions add (apps/cloud/extensions.py): the policy, their routes, what the pages show.
    app.state.extensions = loaded
    app.state.policy = extensions.policy_of(loaded)
    # The agent type packs they bring, checked now (a clash refuses to start).
    app.state.agent_packs = agent_packs.build((pack for ext in loaded for pack in ext.agent_types), built_in=packs.built_in())
    app.include_router(api)
    for ext in loaded:
        for router in ext.routers:
            app.include_router(router, prefix="/api")
    if not settings.selfhost:
        # Account deletion (Auth0, and what extensions hold): a self-hosted server has none.
        app.include_router(account_deletion_router, prefix="/api")
    app.add_exception_handler(browser_links.Refused, browser_links.refused_handler)
    app.add_exception_handler(extensions.Denied, extensions.denied_handler)
    # Wysebee Assistant's /assistant/mcp and its OAuth metadata (apps/cloud/agent_mcp.py), at the site root: before
    # the SPA's catch-all.
    app.include_router(mcp_router)
    # Pages shared publicly, at ``/s/<key>`` (apps/cloud/workspace_pages.py): before the SPA's catch-all.
    app.include_router(workspace_pages.share_router)
    if settings.share_url:
        # …on a domain of their own, which answers with nothing else.
        app.middleware("http")(workspace_pages.share_host_gate)
    if mock_auth:
        app.include_router(mock_auth_router)
        app.middleware("http")(localhost_cors)
    if settings.selfhost:
        # No landing page to sign in from: the site is the web app.
        @app.get("/", include_in_schema=False)
        async def selfhost_root() -> RedirectResponse:
            return RedirectResponse("/app/", status_code=307)

    dist = ui_dist or settings.ui_dist
    _mount_web_app(app, dist / "app", settings)
    _mount_spa(app, dist)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level)
