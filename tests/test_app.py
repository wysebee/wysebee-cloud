import asyncio
import contextlib
import logging
import runpy
import types
import warnings

import pytest

from apps.cloud import __version__, main
from apps.cloud.config import Settings
from apps.cloud.main import migration_head
from tests.conftest import UNREACHABLE_DB_URL as UNREACHABLE, make_client, needs_db


def test_health(ui_dist):
    with make_client(ui_dist) as client:
        assert client.get("/api/health").json() == {"status": "ok"}


def test_spa_root_and_fallback(ui_dist):
    with make_client(ui_dist) as client:
        for path in ("/", "/some/client/route"):
            r = client.get(path)
            assert r.status_code == 200
            assert "id=root" in r.text


def test_assets_served(ui_dist):
    with make_client(ui_dist) as client:
        assert client.get("/assets/app.js").text == "console.log(1)"


def test_unknown_api_is_404(ui_dist):
    with make_client(ui_dist) as client:
        assert client.get("/api/nope").status_code == 404


def test_ready_reports_database_down(ui_dist):
    with make_client(ui_dist) as client:
        r = client.get("/api/ready")
        assert r.status_code == 503
        assert r.json()["database"] == "down"


@needs_db
def test_ready_with_database(db_client):
    r = db_client.get("/api/ready")
    assert r.status_code == 200, r.text
    assert r.json()["database"] == "up"


def test_plain_postgres_url_gets_psycopg_driver():
    assert Settings(database_url="postgres://u:p@h/d").database_url == "postgresql+psycopg://u:p@h/d"


def test_healthcheck_reports_the_database_down(ui_dist):
    with make_client(ui_dist) as client:
        r = client.get("/api/healthcheck")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "down" and body["version"] == __version__
    assert body["database"]["status"] == "down"
    # The error's type, never its message (which names hosts).
    assert body["database"]["error"] == "OperationalError"
    assert r.headers["cache-control"] == "no-store"


def test_the_build_knows_its_migration_head():
    assert migration_head() == "0087"


@needs_db
def test_healthcheck_with_a_migrated_database(db_client):
    r = db_client.get("/api/healthcheck")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"]["status"] == "up"
    head = migration_head()
    assert body["database"]["migration"] == {"current": head, "head": head, "up_to_date": True}
    assert isinstance(body["uptime_seconds"], int)


# --- the web app at /app/ --------------------------------------------------------------


def _with_web_app(ui_dist):
    app_dir = ui_dist / "app"
    (app_dir / "assets").mkdir(parents=True)
    (app_dir / "index.html").write_text("<div id=web-app></div>")
    (app_dir / "assets" / "index-abc.js").write_text("console.log('web')")
    return ui_dist


def test_the_web_app_is_served_at_app_with_its_own_fallback(ui_dist):
    with make_client(_with_web_app(ui_dist), auth_issuer="https://wysebee-test.us.auth0.com/") as client:
        r = client.get("/app", follow_redirects=False)
        assert r.status_code == 308 and r.headers["location"] == "/app/"
        for path in ("/app/", "/app/agents/helper", "/app/settings"):
            r = client.get(path)
            assert r.status_code == 200 and "web-app" in r.text, path
            assert r.headers["cache-control"] == "no-cache"
        asset = client.get("/app/assets/index-abc.js")
        assert asset.text == "console.log('web')"
        assert "immutable" in asset.headers["cache-control"]
        # The landing page is still the landing page.
        assert "id=root" in client.get("/").text


def test_the_web_app_has_a_strict_csp_naming_the_sign_in_provider(ui_dist):
    with make_client(_with_web_app(ui_dist), auth_issuer="https://wysebee-test.us.auth0.com/") as client:
        csp = client.get("/app/").headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "connect-src 'self' https://wysebee-test.us.auth0.com" in csp
    assert "frame-ancestors 'none'" in csp
    # Usage events go to this server, which forwards them: no Mixpanel host (2026-11-15).
    assert "mixpanel" not in csp


def test_the_web_app_csp_names_no_mixpanel_host_even_with_a_project(ui_dist):
    with make_client(_with_web_app(ui_dist), mixpanel_token="abc", mixpanel_api_host="https://api-eu.mixpanel.com/") as client:
        csp = client.get("/app/").headers["content-security-policy"]
    assert "connect-src 'self';" in csp


def test_the_web_app_never_serves_files_outside_itself(ui_dist):
    (ui_dist / "secret.txt").write_text("landing-only")
    with make_client(_with_web_app(ui_dist)) as client:
        r = client.get("/app/..%2Fsecret.txt")
    assert "landing-only" not in r.text


def test_without_a_web_app_build_app_is_just_the_landing_page(ui_dist):
    with make_client(ui_dist) as client:
        r = client.get("/app/")
    assert r.status_code == 200 and "id=root" in r.text


# --- the rest of main.py -------------------------------------------------------------

@pytest.fixture
def logs(caplog, monkeypatch):
    """main.py's log lines. The migrations' fileConfig (tests/conftest.py's migrated_db_url)
    disables loggers that already exist; this one is turned back on for the test."""
    monkeypatch.setattr(main.logger, "disabled", False)
    with caplog.at_level(logging.INFO, "wysebee.cloud"):
        yield caplog


def test_info_names_the_service(ui_dist):
    with make_client(ui_dist) as client:
        assert client.get("/api/v1/info").json() == {"name": "wysebee-cloud", "version": __version__, "status": "ok"}


def test_without_the_migration_scripts_there_is_no_head(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ALEMBIC_INI", tmp_path / "alembic.ini")
    main.migration_head.cache_clear()
    try:
        assert migration_head() is None
    finally:
        main.migration_head.cache_clear()


class _Db:
    """A database that answers the migration question with a given revision."""

    def __init__(self, revision):
        self.revision = revision

    async def migration_revision(self):
        return self.revision


def test_healthcheck_reports_a_schema_behind_the_head_as_degraded(ui_dist):
    with make_client(ui_dist) as client:
        real = client.app.state.db
        client.app.state.db = _Db("0001")
        try:
            r = client.get("/api/healthcheck")
        finally:
            client.app.state.db = real
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["database"]["migration"] == {"current": "0001", "head": migration_head(), "up_to_date": False}


def test_without_a_build_there_is_no_spa(tmp_path, logs):
    with make_client(tmp_path) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/health").json() == {"status": "ok"}
    assert "SPA not built" in logs.text


def test_real_files_in_the_build_are_served_as_they_are(ui_dist):
    (ui_dist / "favicon.svg").write_text("<svg/>")
    with make_client(ui_dist) as client:
        r = client.get("/favicon.svg")
        assert r.text == "<svg/>" and "cache-control" not in r.headers
        # Outside the build: the shell, never the file.
        (ui_dist.parent / "outside.txt").write_text("secret")
        assert "secret" not in client.get("/..%2Foutside.txt").text


class _Sessions:
    """``db.sessionmaker()`` that hands out a stand-in session."""

    def sessionmaker(self):
        @contextlib.asynccontextmanager
        async def session():
            yield object()

        return session()


class _Stop(Exception):
    pass


def _one_round(monkeypatch):
    async def stop(_seconds):
        raise _Stop

    monkeypatch.setattr(main, "asyncio", types.SimpleNamespace(sleep=stop))


def _removing(count):
    async def purge(*_args):
        return count

    return purge


def test_the_browser_call_purge_logs_what_it_removed(monkeypatch, logs):
    _one_round(monkeypatch)
    monkeypatch.setattr(main.browser_links, "purge", _removing(5))
    with pytest.raises(_Stop):
        asyncio.run(main._purge_turns_often(_Sessions(), Settings(database_url=UNREACHABLE)))
    assert "purged 5 browser calls" in logs.text


def test_run_as_a_script_it_serves_with_uvicorn(monkeypatch):
    import uvicorn

    served = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.update(app=app, **kw))
    monkeypatch.setenv("WYSEBEE_PORT", "8123")
    main.get_settings.cache_clear()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # "found in sys.modules": it is, as a library
            runpy.run_module("apps.cloud.main", run_name="__main__")
    finally:
        main.get_settings.cache_clear()
    assert served["port"] == 8123 and served["host"] == main.get_settings().host
    assert served["app"].title == "Wysebee Cloud"


def test_chat_and_the_old_credits_address_are_gone(ui_dist):
    # No chat and no AI model in Wysebee Cloud (2026-11-14); credits are at /tool-calls.
    with make_client(ui_dist) as client:
        for method, path in (("POST", "/api/v1/chat"), ("GET", "/api/v1/chat/model"), ("POST", "/api/v1/turns"), ("GET", "/api/v1/chat-turns"),
                             ("POST", "/api/v1/reports"), ("GET", "/api/v1/agent-keys"), ("POST", "/api/v1/agents/sky/chat"), ("GET", "/api/v1/ai-turns")):
            assert client.request(method, path).status_code in (404, 405), path


def test_a_cancel_turned_into_a_query_error_still_stops_a_background_loop():
    """psycopg answers a cancel that lands mid-query with an ordinary error: a loop that skips
    a round on any error must stop all the same, or the app's shutdown waits for it for ever."""
    import asyncio

    from apps.cloud.db import raise_if_cancelled

    rounds = []

    async def loop() -> None:
        while True:
            try:
                rounds.append(1)
                await asyncio.sleep(10)
            except asyncio.CancelledError as exc:
                try:
                    raise RuntimeError("canceling statement due to user request") from exc
                except Exception as err:  # what the loops catch
                    raise_if_cancelled(err)
            await asyncio.sleep(3600)

    async def run() -> bool:
        task = asyncio.create_task(loop())
        await asyncio.sleep(0)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=2)
        return bool(done) and task.cancelled()

    assert asyncio.run(run()) and rounds == [1]
