"""The browser link: the Wysebee Agent browser extension, paired with Wysebee Cloud and
driven through it.

The contract is ``docs/protocol/browser-extension.md`` (and, for the other
kinds, wysebee-agent-osx's, -win's and -android's) — the agent apps and this module are each
built against it, and **a change there is a change in both**. In short::

    extension ──POST /start──▶ a link, nobody's, with a pairing code it shows
    Wysebee   ──POST /claim (the code)──▶ the link is the person's, and a new agent is made for it
                                          (or, 2026-11-13, an agent of its kind with none is given it)
    driver    ──POST /{id}/calls──▶ browser_calls row ──▶ the extension's POST /{id}/poll
              ◀── the result ────                     ◀── POST /{id}/calls/{call}/result

- **The extension pairs with the cloud only**, and identifies itself with a secret it made:
  ``Authorization: Bearer <secret>``, compared in constant time with the SHA-256 kept on
  its ``browser_links`` row. The secret itself is never stored. ``/start`` alone takes no
  credential, so it is bounded: small bodies, codes that run out
  (``browser_link_code_seconds``) and are purged, a cap on links waiting for their code
  (``browser_link_pending_max``) and so many starts a minute from one address
  (``browser_link_starts_per_minute``, on each replica).
- **Pairing creates a new agent** (or reconnects one: below), here — through the agents' own
  write path (apps/cloud/agents.py), so the change feed tells the person's open pages at once.
  The link holds the agent by id (``agent_id``: renaming the agent keeps it paired, deleting it
  deletes the link). Its settings name the link as ``browser_link``, **which only the claim
  sets**: an agent save keeps what the links table says whatever is sent (:func:`keep_link`).
- **Only that agent drives that browser.** A driver is any signed-in client of the account
  that owns the link — the website, or this server's own agent loop in process (:func:`run_op`), which also names the agent and is refused for any other.
  Another account's link is simply not there (404).
- **Postgres is the queue**, as for the relay (apps/cloud/relay.py), whose listener hub and
  channel this shares: ``bl:<link id>`` wakes that extension's poll, ``bc:<call id>`` the
  driver waiting on a call. Without ``LISTEN``, waiters look again every second.
- **Offline is said at once**: an extension that has not polled within
  ``browser_link_online_seconds`` gets no call. One not taken within
  ``browser_link_claim_seconds`` fails, and one not answered within its own
  ``timeout_seconds`` fails too (the extension is told to stop it).
- **Kept briefly.** A call's row holds its arguments and result — the person's pages. It is
  deleted once its result is read, and ``browser_link_keep_seconds`` after it was made
  regardless (:func:`purge`).
- **Gone means unpaired**: unpairing (either side), deleting the agent or deleting the
  account deletes the link, and the extension's next poll is a 401 ``unpaired``.

Errors are ``{"detail": "<English sentence>", "code": "<code>"}`` (:class:`Refused`).

**A taken name is settled where the code is typed** (2026-11-13, the owner's call; the
protocol's section of that name): a claim refused for the name (``name_taken``,
``invalid_name``, ``reserved``, ``too_many``) **keeps the code good** — the device goes on
showing it — and ``name_taken`` says which agent has the name and whether it can be
``reconnect``-ed: only when its ``paired_kind`` (``CONFIG``, set by the claim, kept after
unpairing, never by a save: :func:`keep_link`) is this link's kind and no link holds it now.
The person claims again with the same code, with another ``name``, or with ``reconnect``:
the link is then given to that agent, which keeps everything else.

**One link mechanism, two kinds** (2026-11-12; ``docs/protocol/computer.md``,
which builds on the extension's): a link's ``kind`` is ``browser`` — everything above — or
``osx``, **Wysebee Agent for Mac**, which runs the agent's "Run local program" tools on
that Mac. The same routes are served under ``/api/v1/agent-links`` and
``/api/v1/browser-links``, and a link of either kind works at either. What differs for an
``osx`` link:

- its agent's ``CONFIG`` names it as ``mac_link`` (no ``browser_access``);
- its poll says which of the agent's programs that Mac has allowed (``approved``, kept on
  the link and shown in ``Link``) and is answered the agent's programs (its ``program``
  tools, apps/cloud/user_tools.py)
  (``programs``) — at once when they change (:func:`wake_for_agent`);
- it takes only the ops ``ping``, ``local_run``, ``skill_run`` and ``folder_call`` — sent by
  this server's own agent loop (apps/cloud/agent_chat/mac.py, through :func:`run_op`;
  :func:`paired_mac`).

**Skill scripts** (2026-11-12, later the same day; the Mac protocol's "Skill scripts"): a
skill's declared scripts run only for an agent made by pairing Wysebee Agent for Mac, on that
Mac. Its ``paired`` poll answer also carries ``skills`` — each skill the agent has
(apps/cloud/skills.py), by its current name, with its file list (:func:`_skills`) — and is
woken when the agent's skills, or one of them, change (:func:`wake_for_agent`,
:func:`wake_for_skill`). Renaming a skill renames it in the poll. The Mac
says which it has allowed (``approved_skills``, kept on the link and shown in ``Link``) and
downloads a skill's files with its own secret (:func:`skill_file`): only files of a skill
its agent has, from the person's skill file store (apps/cloud/skill_files.py).

**Local folders** (2026-11-12, later again; the Mac protocol's "Local folders"): Local Folder
is a Mac agent's only. The person picks each folder in the Mac app,
whose poll says them (``folders``, :func:`_folders`); the cloud keeps the list on the link
(``Link.folders``) and, when it changed, writes it into the agent's settings as
``mac_folders`` through the agents' write path (:func:`_write_folders`), so every device
hears of it. ``mac_folders`` is set only here: an agent save keeps what the link says
(:func:`keep_link`), and an agent without a Mac has none. An ``osx`` link takes
``folder_call`` too.

**A phone** (2026-11-15; ``docs/protocol/android.md``): a third kind,
``android``, **Wysebee Agent for Android** (type ``wysebee-android``, migration 0059), which
lets the agent operate the phone — read the screen, tap, type, swipe, open apps — in the apps
the person allowed there. Its link is held in ``android_link``; its poll carries only calls,
as a browser's does; what it may do is its type's ``ops`` and the phone's own checks.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi import HTTPException
from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apps.cloud import agent_packs, agent_types, changes, skills, tool_kinds, traffic, user_tools
from apps.cloud.auth import User, current_user
from apps.cloud.config import Settings
from apps.cloud.asset_storage import AssetStore
from apps.cloud.models import AgentLinkOffer, AgentSkill, BrowserCall, BrowserLink, CloudSkillFile, UserAgent
from apps.cloud.relay import RECHECK_SECONDS, RelayHub, _notify, _wait, public_base

logger = logging.getLogger("wysebee.cloud.browser_links")

# The routes, served under both prefixes (:data:`router`, at the end of this module).
links = APIRouter()
PREFIXES = ("/v1/browser-links", "/v1/agent-links")

# A link's legacy kind, by its type's ``link_field`` (apps/cloud/agent_types.py): the
# Wysebee Agent browser extension, or Wysebee Agent for Mac. What a link may do is its type's
# row (2026-11-13, later); the code knows the two places a link is held, and what a Mac's
# poll carries.
BROWSER = "browser"
OSX = "osx"
# Wysebee Agent for Android (2026-11-15): a link held in `android_link`, whose poll carries
# only calls, as a browser's does.
ANDROID = "android"  # served by its pack (apps/cloud/packs/android.py)

ID = r"^[0-9a-f]{32}$"
LinkId = Path(pattern=ID)
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
SECRET_HASH = re.compile(r"^[0-9a-f]{64}$")
# A QR code's offer (2026-11-17): 32 random bytes, base64url without padding.
OFFER_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
# Offers one person may have open at once; making another drops the oldest.
MAX_OFFERS = 5
# What a driver could ask of the browser before agent types — the Wysebee Chrome Agent type's
# seeded `ops` (migration 0050), which its row now says: the old link's BROWSER_OPS, less
# `configure` and `shutdown` (docs/protocol/browser-extension.md, "Driving the browser"); `browser_mock` since
# migration 0081, `browser_record` since 0082.
OPS = frozenset({
    "ping", "browser_tabs", "browser_activate", "browser_snapshot", "browser_screenshot",
    "browser_navigate", "browser_click", "browser_type", "browser_open_tab",
    "browser_close_tab", "browser_scroll", "browser_drag", "browser_mouse",
    "browser_bookmarks", "browser_console", "browser_network", "browser_network_body",
    "browser_evaluate", "browser_viewport", "browser_inspect", "browser_audit",
    "browser_performance", "browser_reload", "browser_mock", "browser_record", "recording_saved",
})
# An agent's handle, how many a person may have, its label's length, and handles kept back.
MAX_HANDLE = 30
MAX_AGENTS = 20
MAX_LABEL = 60
WYSEBEE = "wysebee"
RESERVED = ("pairing", "camera")
# The config field naming an agent's link, by the link's kind.
LINK_FIELDS = tool_kinds.LINK_FIELDS
# The type of device the agent was made for — its key (2026-11-13; ``browser`` / ``osx``
# before agent types) and id: set by the claim, kept when it is unpaired, never by a save —
# what a reconnect is checked against.
PAIRED_KIND = "paired_kind"
TYPE_ID = "agent_type_id"
UUID_TEXT = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NO_TYPE = "This device's type is not one Wysebee knows now. Update the Wysebee Agent app or extension, then start again."
# An osx link: the names of the programs its Mac has allowed, and the agent's programs.
MAX_PROGRAMS = 20
MAX_PROGRAM_NAME = 40
# An osx link: the agent's skills (apps/cloud/skills.py, at most 50), and the names of
# those its Mac has allowed.
MAX_SKILLS = 50
MAX_SKILL_NAME = 40
# An osx link: the folders its Mac offers the agent (Local Folder), kept on the link and
# written into the agent's settings as `mac_folders`, which only the cloud sets.
FOLDERS_FIELD = "mac_folders"
MAX_FOLDERS = 20
FOLDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_FOLDER_NAME = 60
MAX_FOLDER_TEXT = 1000
FOLDER_MODES = ("read", "write")
# How many calls one poll hands the extension.
CLAIM_BATCH = 5
# Small bodies: /start, /claim.
SMALL_BODY = 4096
# A poll's status: small, but a Mac's may list 20 folders with their paths and descriptions.
POLL_BODY = 256 * 1024

UNPAIRED = "This browser is not paired with Wysebee any more."
EXPIRED = "The pairing code ran out before it was typed in Wysebee. Start again."
CODE_UNKNOWN = "That code is not one Wysebee is waiting for. Check it in the extension: a code works once, for 10 minutes."
TOO_MANY_TRIES = "Too many wrong codes. Wait a few minutes, then try again."
OFFER_UNKNOWN = "That QR code is not one Wysebee is waiting for: it works once, for 10 minutes. Show a new one in Wysebee."
NO_LINK = "That browser is not paired with this account."
OFFLINE = "The browser is not reachable: it is closed, or the Wysebee Agent extension is off."
NOT_REACHED = "The browser could not be reached: it did not take this in time. Is it open, with the Wysebee Agent extension on?"
TIMED_OUT = "The browser did not finish this within {seconds:.0f} seconds, so it was stopped. It may have been waiting for the person to approve it in the browser."
TOO_BIG = "The result was too big to send back (screenshots count): ask for less at a time."
NO_ANSWER = "The browser refused this, without saying why."
NOT_AN_OP = "'op' is not one of the browser's operations."
CALL_GONE = "That call is no longer there: it was cancelled, read already, or it expired."
FILE_UNKNOWN = "That file is not one of this agent's skills' files."
REFUSALS = {
    "invalid_name": "No agent name can be made from that: use letters or digits, starting with a letter.",
    "reserved": "That name is kept for Wysebee's own use: choose another.",
    "name_taken": "There is already an agent with that name: choose another, or reconnect to it if Wysebee offers to.",
    "too_many": f"You have {MAX_AGENTS} agents already: delete one in Wysebee to add another.",
}


async def device_counts(session: AsyncSession, settings: Settings, user_id: str) -> tuple[int, int]:
    """``user_id``'s registered devices — their paired links — and how many of those are online
    now (polled within ``browser_link_online_seconds``, not switched off)."""
    since = _now() - timedelta(seconds=settings.browser_link_online_seconds)
    row = (await session.execute(
        select(func.count(), func.count().filter(BrowserLink.last_seen >= since, BrowserLink.paused.is_(False))).where(BrowserLink.user_id == user_id)
    )).one()
    return int(row[0]), int(row[1])


def _said(sentence: str, kind: str | None) -> str:
    """``sentence`` as it is said of a link of ``kind``: in the words of the pack serving that
    kind (apps/cloud/agent_packs.py: the computer's, the phone's), if any."""
    return agent_packs.words(kind).get(sentence, sentence)


class Refused(Exception):
    """A refusal in the protocol's shape: ``{"detail": sentence, "code": code}``, and what
    else it says (``name_taken``'s ``agent`` and ``reconnect``)."""

    def __init__(self, status: int, code: str, detail: str, more: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail, self.more = status, code, detail, more or {}


async def refused_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, Refused)
    return JSONResponse({"detail": exc.detail, "code": exc.code, **exc.more}, status_code=exc.status)


class Tries:
    """At most ``limit`` wrong pairing codes in ``window`` seconds for each account, on
    this replica."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self.failed: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, who: str, now: float | None) -> deque[float]:
        now = time.monotonic() if now is None else now
        recent = self.failed[who]
        while recent and recent[0] <= now - self.window:
            recent.popleft()
        if not recent:
            del self.failed[who]
        return recent

    def allowed(self, who: str, now: float | None = None) -> bool:
        return len(self._recent(who, now)) < self.limit

    def failure(self, who: str, now: float | None = None) -> None:
        self._recent(who, now)
        self.failed[who].append(time.monotonic() if now is None else now)


# --- small things --------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(t: datetime | None) -> str | None:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z") if t else None


def _link_key(link_id: str) -> str:
    return f"bl:{link_id}"


def _call_key(call_id: str) -> str:
    return f"bc:{call_id}"


def _wake(state: Any, keys: list[str]) -> None:
    hub: RelayHub = state.relay_hub
    for key in keys:
        hub.wake(key)


def _no_nul(value: Any) -> Any:
    """``value`` without NUL characters, which PostgreSQL refuses in jsonb."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_no_nul(v) for v in value]
    if isinstance(value, dict):
        return {_no_nul(k): _no_nul(v) for k, v in value.items()}
    return value


async def _body(request: Request, max_bytes: int) -> dict[str, Any]:
    """The request's JSON object, read no further than ``max_bytes``."""
    too_big = Refused(413, "too_big", f"At most {max_bytes} bytes.")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise too_big
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        request.state.body_bytes = len(data)
        if len(data) > max_bytes:
            raise too_big
    try:
        value = json.loads(bytes(data) or b"{}")
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise Refused(422, "invalid_body", "Send a JSON object.")
    return value


def _line(value: Any, most: int) -> str | None:
    """``value`` as one trimmed line of 1 to ``most`` characters, or None."""
    if not isinstance(value, str):
        return None
    text_value = value.strip()
    if not text_value or len(text_value) > most or any(c in text_value for c in "\n\r\x00"):
        return None
    return text_value


def _short(value: Any, most: int = 32) -> str | None:
    """An optional short label (a browser's name, a version), or None when it is not one."""
    return value.strip()[:most].replace("\x00", "") if isinstance(value, str) else None


def handle_of(name: str) -> str | None:
    """The agent handle made from what the person typed: lowercase; every run of characters
    other than ``a-z0-9`` a ``-``; trimmed of ``-``; at most 30; starting with a letter.
    None when none can be made."""
    handle = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:MAX_HANDLE].strip("-")
    return handle if handle and handle[0].isalpha() and handle[0].isascii() else None


def clean_code(value: Any) -> str | None:
    """A typed code — with or without the dash, any case — as stored, or None."""
    if not isinstance(value, str):
        return None
    code = re.sub(r"[\s-]", "", value).upper()
    return code if len(code) == CODE_LENGTH and all(c in CODE_ALPHABET for c in code) else None


def _client_address(request: Request) -> str:
    """Who is asking, for the limit on starts: the address the ingress saw (the last hop it
    added to X-Forwarded-For), else the connection's."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def _connected(link: BrowserLink, now: datetime, s: Settings) -> bool:
    return link.last_seen is not None and now - link.last_seen <= timedelta(seconds=s.browser_link_online_seconds)


BROWSER_OFFLINE = "browser_offline"


async def paired_link(session: AsyncSession, user_id: str, config: dict[str, Any], kind: str, link_field: str) -> BrowserLink | None:
    """The device of a kind an agent was made by pairing (the link its ``link_field`` holds), if
    it is still paired — an agent type pack's (apps/cloud/agent_packs.py)."""
    link_id = config.get(link_field)
    if not (isinstance(link_id, str) and link_id.strip()):
        return None
    link = await session.get(BrowserLink, link_id)
    return link if link is not None and link.user_id == user_id and link.kind == kind else None


async def offline_reason(session: AsyncSession, settings: Settings, user_id: str, agent: str, config: dict[str, Any]) -> str | None:
    """Why an agent that lives in its device cannot answer now, in words to show — or None: as
    the pack serving its paired device says (apps/cloud/agent_packs.py; the browser's: such an
    agent lives in its browser, 2026-11-13, and is offline while the extension is not polling or
    is paused — its Connect AI app tab says so, apps/cloud/agent_mcp.py). An agent whose device
    was unpaired answers as any other in the cloud."""
    now = _now()
    for pack in agent_packs.known().packs:
        link = await paired_link(session, user_id, config, pack.kind, pack.link_field)
        if link is not None:
            said = pack.offline_reason(link, agent, not link.paused and _connected(link, now, settings))
            if said is not None:
                return said
    return None


def _link_out(link: BrowserLink, now: datetime, s: Settings, kind: agent_types.AgentType | None, agent: str | None) -> dict[str, Any]:
    """A link as the apps read it: its agent (``agent_id``, and ``agent`` its handle now), its
    ``agent_type`` (``{id, key, name}``, or null for a type no longer known) and, for readers
    from before types, its ``kind``."""
    return {
        "id": link.id, "agent_id": link.agent_id, "agent": agent, "label": link.name, "browser": link.browser,
        "extension_version": link.extension_version,
        "status": "connected" if _connected(link, now, s) else "offline",
        "paused": link.paused, "last_seen": _iso(link.last_seen), "created_at": _iso(link.created_at),
        "agent_type": kind.out() if kind is not None else None,
        "kind": kind.kind if kind is not None else link.kind, "approved": [], "approved_skills": [], "folders": [],
        # What its kind's pack adds (the computer's: the programs and skills it allowed, its folders).
        **_view_of(link),
    }


def _view_of(link: BrowserLink) -> dict[str, Any]:
    pack = agent_packs.known().of_kind(link.kind)
    return pack.link_view(link) if pack is not None else {}


def _approved(value: Any, most: int = MAX_PROGRAMS, longest: int = MAX_PROGRAM_NAME) -> list[str] | None:
    """The names a Mac says it has allowed — programs: at most 20, each one line of at most 40
    characters; skills: at most 50 of 40 — or None when that is not what was sent."""
    if not isinstance(value, list) or len(value) > most:
        return None
    names = [_line(v, longest) for v in value]
    return None if any(n is None or n != v for n, v in zip(names, value)) else [n for n in names if n is not None]


def _folder(value: Any) -> dict[str, str] | None:
    """One folder as a Mac reports it — ``{id, name, path, mode, description}`` — or None
    when it is not one."""
    if not isinstance(value, dict):
        return None
    folder_id, path, mode = value.get("id"), value.get("path"), value.get("mode")
    name = _line(value.get("name"), MAX_FOLDER_NAME)
    description = value.get("description", "")
    texts_ok = all(isinstance(t, str) and len(t) <= MAX_FOLDER_TEXT and "\x00" not in t for t in (path, description))
    if not isinstance(folder_id, str) or not FOLDER_ID.match(folder_id) or name is None or not texts_ok or mode not in FOLDER_MODES:
        return None
    return {"id": folder_id, "name": name, "path": path, "mode": mode, "description": description}


def _folders(value: Any) -> list[dict[str, str]] | None:
    """The folders a Mac says it offers, in its order: malformed entries (and a second entry
    with an id already given) dropped, at most 20 — or None when no list was sent, which
    keeps what is stored."""
    if not isinstance(value, list):
        return None
    out: list[dict[str, str]] = []
    for entry in value:
        folder = _folder(entry)
        if folder is not None and all(f["id"] != folder["id"] for f in out):
            out.append(folder)
    return out[:MAX_FOLDERS]


async def _programs(session: AsyncSession, user_id: str, agent_id: str | None) -> list[dict[str, Any]]:
    """The programs of the person's agent (its ``program`` tools, apps/cloud/user_tools.py),
    in its order: at most 20."""
    owned = await user_tools.of_agent(session, user_id, agent_id)
    return [user_tools.program_row(t) for t in owned if t.kind == user_tools.PROGRAM][:MAX_PROGRAMS]


async def _skills(session: AsyncSession, user_id: str, agent_id: str | None) -> list[dict[str, Any]]:
    """The skills of the person's agent, in its order (apps/cloud/skills.py): ``[{"name",
    "files"}]``, each file ``{path, id, size}``."""
    return [
        {"name": s.name, "files": [{"path": f["path"], "id": f["id"], "size": f["size"]} for f in s.files or [] if isinstance(f, dict)]}
        for s in (await skills.of_agent(session, user_id, agent_id))[:MAX_SKILLS]
    ]


# --- the agent's settings --------------------------------------------------------------------


async def keep_link(session: AsyncSession, user_id: str, agent_id: str, stored: UserAgent | None, config: dict[str, Any]) -> dict[str, Any]:
    """An agent's settings as they are saved (apps/cloud/agents.py ``save``): its
    ``browser_link``, ``mac_link`` and ``android_link`` are what the links table says — the link
    made for it, under its kind's field, or none — whatever was sent, and so is ``mac_folders``:
    a Mac's folders as its link holds them, none for any other agent. Only the claim pairs an
    agent with a device, and only its Mac's poll says its folders. So are :data:`PAIRED_KIND`
    and :data:`TYPE_ID` (2026-11-13): the ``stored`` ones, kept after the link is gone; else
    its link's type's key and id; else none. A Mac's poll is woken, so the agent's programs
    show there at once."""
    mine = (await session.execute(
        select(BrowserLink.id, BrowserLink.kind, BrowserLink.folders, BrowserLink.agent_type_id).where(BrowserLink.user_id == user_id, BrowserLink.agent_id == agent_id)
    )).all()
    registry = agent_packs.known()
    waking = agent_packs.kinds_waking("agent")
    await _notify(session, [_link_key(link.id) for link in mine if link.kind in waking])
    fields = tool_kinds.link_fields()
    says: dict[str, Any] = {fields[link.kind]: link.id for link in mine}
    for link in mine:
        pack = registry.of_kind(link.kind)
        if pack is not None:
            says.update(pack.held_settings(link))
    kept = dict(stored.config or {}) if stored is not None else None
    linked = await agent_types.by_id(session, mine[0].agent_type_id) if mine else None
    key, type_id = _paired_kind(kept), _type_id(kept)
    if key is None and linked is not None:
        key, type_id = linked.key, linked.id
    if key is not None:
        says[PAIRED_KIND] = key
    if type_id is not None:
        says[TYPE_ID] = type_id
    pinned = (*fields.values(), *(f for p in registry.packs for f in p.cloud_only_fields), PAIRED_KIND, TYPE_ID)
    return {**{k: v for k, v in config.items() if k not in pinned}, **says}


def _paired_kind(config: dict[str, Any] | None) -> str | None:
    """The type (its key) of device the agent was made for, as its settings keep it, or None."""
    key = (config or {}).get(PAIRED_KIND)
    return key if isinstance(key, str) and agent_types.KEY.match(key) else None


def _type_id(config: dict[str, Any] | None) -> str | None:
    """That type's id, as its settings keep it — only beside its key — or None."""
    type_id = (config or {}).get(TYPE_ID)
    return type_id if _paired_kind(config) is not None and isinstance(type_id, str) and UUID_TEXT.match(type_id) else None


async def wake_for_agent(session: AsyncSession, user_id: str, *agent_ids: str) -> None:
    """These agents' skills, programs or names changed: wake the poll of each one's device of a
    kind whose pack wakes on it (the computer's), so the device hears of it at once."""
    if not agent_ids:
        return
    rows = (await session.scalars(
        select(BrowserLink.id).where(BrowserLink.user_id == user_id, BrowserLink.kind.in_(agent_packs.kinds_waking("agent")), BrowserLink.agent_id.in_(agent_ids))
    )).all()
    await _notify(session, [_link_key(i) for i in rows])


async def wake_for_skill(session: AsyncSession, user_id: str, skill_id: str) -> None:
    """One of the person's skills was changed or deleted: wake the poll of every Mac whose
    agent has it."""
    agents = (await session.scalars(select(AgentSkill.agent_id).where(AgentSkill.user_id == user_id, AgentSkill.skill_id == skill_id))).all()
    await wake_for_agent(session, user_id, *agents)


async def delete_for_agent(session: AsyncSession, user_id: str, agent_id: str) -> None:
    """An agent is being deleted: its link goes too (and its calls), and the device hears on
    its next poll."""
    gone = (await session.execute(
        delete(BrowserLink).where(BrowserLink.user_id == user_id, BrowserLink.agent_id == agent_id).returning(BrowserLink.id)
    )).scalars().all()
    await _notify(session, [_link_key(i) for i in gone])


async def _forget_link(session: AsyncSession, settings: Settings, user_id: str, agent_id: str | None) -> None:
    """Take the link's field (``browser_link``, ``mac_link``…) out of the agent's settings,
    through the agents' write path (the link's row is already deleted in this transaction).
    The agent stays."""
    from apps.cloud import agents

    # The link held its agent (a foreign key), so it is there; keep_link drops the field.
    row = await agents.by_id(session, user_id, agent_id)
    if row is not None:
        await agents.save(session, settings, user_id, row, name=row.name, config=dict(row.config or {}), description=row.description)


async def _write_folders(session: AsyncSession, settings: Settings, user_id: str, row: UserAgent) -> None:
    """Write a Mac's folders into its agent's settings as ``mac_folders``, through the agents'
    write path, in the poll's transaction — the link already holds them, and
    :func:`keep_link` pins what it holds."""
    from apps.cloud import agents

    await agents.save(session, settings, user_id, row, name=row.name, config=dict(row.config or {}), description=row.description)


# --- pairing ----------------------------------------------------------------------------------


async def purge(session: AsyncSession, settings: Settings) -> int:
    """Calls past their keeping time, and links whose code ran out a while ago with nobody
    typing it (kept one more code's time, so the extension hears ``expired``); how many
    calls. Run every few minutes (apps/cloud/main.py)."""
    now = _now()
    removed = (await session.execute(
        delete(BrowserCall).where(BrowserCall.created_at < now - timedelta(seconds=settings.browser_link_keep_seconds))
    )).rowcount or 0  # type: ignore[attr-defined]
    await _purge_pending(session, settings, now)
    # Offers run out with the codes; one that paired is forgotten then too.
    await session.execute(delete(AgentLinkOffer).where(AgentLinkOffer.expires_at < now - timedelta(seconds=settings.browser_link_code_seconds)))
    await session.commit()
    return int(removed)


async def _purge_pending(session: AsyncSession, settings: Settings, now: datetime) -> None:
    await session.execute(
        delete(BrowserLink).where(
            BrowserLink.user_id.is_(None),
            BrowserLink.code_expires_at < now - timedelta(seconds=settings.browser_link_code_seconds),
        )
    )


@links.post("/start")
async def start(request: Request) -> dict[str, Any]:
    """A device begins pairing: a link nobody owns yet, of the type its ``kind`` names (an
    enabled type's key; from builds made before types, ``browser``, ``osx`` or none), and the
    code to show."""
    state = request.app.state
    s: Settings = state.settings
    if not state.browser_link_starts.allow(_client_address(request)):
        raise Refused(429, "too_many_starts", "Too many pairing attempts from here. Wait a minute, then try again.")
    body = await _body(request, SMALL_BODY)
    name = _line(body.get("name"), MAX_LABEL)
    if name is None:
        raise Refused(422, "invalid_name", f"The agent's name must be one line of 1 to {MAX_LABEL} characters.")
    secret_hash = body.get("secret_hash")
    if not isinstance(secret_hash, str) or not SECRET_HASH.match(secret_hash):
        raise Refused(422, "invalid_hash", "'secret_hash' must be the secret's SHA-256: 64 lowercase hex characters.")
    offer = body.get("offer")
    if offer is not None and not (isinstance(offer, str) and OFFER_TOKEN.match(offer)):
        raise Refused(404, "offer_unknown", OFFER_UNKNOWN)
    now = _now()
    owner: str | None = None
    # A scan asking to be reconnected to an agent that already exists (2026-11-18): the device
    # alone would choose to take over that agent's chats, settings and tools, and a device is
    # whatever paired — a modified agent too. So it is not claimed at once: it gets a code, like
    # any device, and the person reconnects it in Wysebee, where they are signed in (the claim's
    # `name_taken`, then `reconnect`). The offer stays good.
    confirm = offer is not None and body.get("reconnect") is True
    async with state.db.sessionmaker() as session, session.begin():
        kind = await agent_types.for_start(session, body.get("kind"))
        if kind is None:
            raise Refused(422, "invalid_kind", "'kind' is not a type of device that can pair now.")
        if confirm:
            known = await session.scalar(
                select(AgentLinkOffer.id).where(AgentLinkOffer.token_hash == hashlib.sha256(offer.encode()).hexdigest(), AgentLinkOffer.used_at.is_(None), AgentLinkOffer.expires_at > now)
            )
            if known is None:
                raise Refused(404, "offer_unknown", OFFER_UNKNOWN)
        elif offer is not None:
            # Taken for this pairing at once, so two scans of one QR code pair once.
            owner = await session.scalar(
                update(AgentLinkOffer)
                .where(AgentLinkOffer.token_hash == hashlib.sha256(offer.encode()).hexdigest(), AgentLinkOffer.used_at.is_(None), AgentLinkOffer.expires_at > now)
                .values(used_at=now)
                .returning(AgentLinkOffer.user_id)
            )
            if owner is None:
                raise Refused(404, "offer_unknown", OFFER_UNKNOWN)
        await _purge_pending(session, s, now)
        # One address holds at most so many waiting codes, so a few cannot fill the table for
        # everyone (2026-11-18). Only the address's SHA-256 is kept, and only while it waits.
        address = hashlib.sha256(_client_address(request).encode()).hexdigest()
        held = await session.scalar(
            select(func.count()).select_from(BrowserLink).where(BrowserLink.user_id.is_(None), BrowserLink.start_address == address, BrowserLink.code_expires_at > now)
        )
        if (held or 0) >= s.browser_link_pending_per_address:
            raise Refused(429, "too_many_starts", "Too many pairing codes are waiting from here. Use one of them, or wait for them to run out.")
        waiting = await session.scalar(select(func.count()).select_from(BrowserLink).where(BrowserLink.user_id.is_(None)))
        if (waiting or 0) >= s.browser_link_pending_max:
            raise Refused(503, "busy", "Too many browsers are pairing right now. Try again in a few minutes.")
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if await session.scalar(select(BrowserLink.id).where(BrowserLink.code == code)) is None:
                break
        link = BrowserLink(
            id=uuid.uuid4().hex, code=code, code_expires_at=now + timedelta(seconds=s.browser_link_code_seconds),
            secret_hash=secret_hash, name=name, browser=_short(body.get("browser")) or "",
            extension_version=_short(body.get("extension_version")) or "", created_at=now, kind=kind.kind, agent_type_id=kind.id, approved=[], approved_skills=[], folders=[],
            start_address=address,
        )
        session.add(link)
    if owner is None or offer is None:
        return {"id": link.id, "code": code, "expires_in": int(s.browser_link_code_seconds)}
    return await _claim_offered(state, owner, offer, link.id, code)


async def _claim_offered(state: Any, owner: str, offer: str, link_id: str, code: str) -> dict[str, Any]:
    """A device started pairing with a QR code's offer: its link is claimed for the person
    who offered it, at once, under the name typed on the device — always as a new agent: a
    device asking to be reconnected gets a code instead (:func:`start`, 2026-11-18). Refused (a
    name taken, say: ``name_taken`` says whether it may be reconnected), the link goes and the
    offer is good again, for the device to start again with another name — or with
    ``reconnect``, for a code the person types in Wysebee; paired, the offer says which agent,
    for Wysebee to show."""
    hashed = hashlib.sha256(offer.encode()).hexdigest()
    try:
        made = await _claim(state, owner, code, None, False)
    except Refused:
        async with state.db.sessionmaker() as session, session.begin():
            await session.execute(delete(BrowserLink).where(BrowserLink.id == link_id, BrowserLink.user_id.is_(None)))
            await session.execute(update(AgentLinkOffer).where(AgentLinkOffer.token_hash == hashed).values(used_at=None))
        raise
    async with state.db.sessionmaker() as session, session.begin():
        await session.execute(
            update(AgentLinkOffer).where(AgentLinkOffer.token_hash == hashed).values(agent_name=made["agent"]["name"], agent_label=made["agent"]["label"])
        )
    return {"id": link_id, "code": None, "expires_in": 0, "agent": made["agent"]}



async def _name_taken(session: AsyncSession, user_id: str, handle: str, link: BrowserLink, kind: agent_types.AgentType) -> tuple[dict[str, Any], bool]:
    """What ``name_taken`` says of the agent holding ``handle`` — its name and label — and
    whether ``link`` may be given to it: only when it was made for this type of device (the
    same key: one paired from Chrome is not reconnected from Edge) and no link holds it now."""
    from apps.cloud import agents

    row = await agents.find(session, user_id, handle)
    config = dict(row.config or {}) if row is not None else None
    label = _line((config or {}).get("label"), MAX_LABEL) or ("Wysebee" if handle == WYSEBEE else handle)
    held = await session.scalar(select(BrowserLink.id).where(BrowserLink.user_id == user_id, BrowserLink.agent_id == row.agent_id, BrowserLink.id != link.id)) if row is not None and row.agent_id else None
    return {"name": handle, "label": label}, row is not None and row.agent_id is not None and _paired_kind(config) == kind.key and held is None


@links.post("/claim")
async def claim(request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
    """The person typed the device's code in Wysebee: the link is theirs, and a new agent is
    made for it — the only one that drives that browser (or runs on that Mac). A name
    refused keeps the code good (2026-11-13): the person claims again with the same code,
    with another ``name`` or with ``reconnect`` (the module's docstring)."""
    state = request.app.state
    tries: Tries = state.browser_link_tries
    body = await _body(request, SMALL_BODY)
    if not tries.allowed(user.sub):
        raise Refused(429, "too_many_tries", TOO_MANY_TRIES)
    try:
        return await _claim(state, user.sub, clean_code(body.get("code")), body.get("name"), body.get("reconnect") is True)
    except Refused as r:
        if r.code == "code_unknown":
            tries.failure(user.sub)
        raise


@links.post("/offers")
async def make_offer(request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
    """A pairing offered here, for a device to scan as a QR code (2026-11-17): ``{id, token,
    expires_in, cloud}``. Wysebee shows the token as a QR code; a device that starts pairing with
    it is claimed for this person at once. One use, ten minutes; at most five open at once.
    ``cloud`` is this site's public address (``public_url``, else the request's forwarded host):
    the QR code names it, and the device pairs there (2026-11-22)."""
    state = request.app.state
    s: Settings = state.settings
    now = _now()
    token = secrets.token_urlsafe(32)
    offer = AgentLinkOffer(id=uuid.uuid4().hex, user_id=user.sub, token_hash=hashlib.sha256(token.encode()).hexdigest(), created_at=now, expires_at=now + timedelta(seconds=s.browser_link_code_seconds))
    async with state.db.sessionmaker() as session, session.begin():
        await session.execute(delete(AgentLinkOffer).where(AgentLinkOffer.user_id == user.sub, AgentLinkOffer.expires_at <= now))
        mine = (await session.execute(select(AgentLinkOffer.id).where(AgentLinkOffer.user_id == user.sub).order_by(AgentLinkOffer.created_at.desc()))).scalars().all()
        if len(mine) >= MAX_OFFERS:
            await session.execute(delete(AgentLinkOffer).where(AgentLinkOffer.id.in_(mine[MAX_OFFERS - 1:])))
        session.add(offer)
    return {"id": offer.id, "token": token, "expires_in": int(s.browser_link_code_seconds), "cloud": public_base(request)}


@links.get("/offers/{offer_id}")
async def offer_state(request: Request, offer_id: str = Path(pattern=ID), user: User = Depends(current_user)) -> dict[str, Any]:
    """How an offer stands: ``waiting`` (for a device to scan it), ``paired`` (with ``agent``)
    or ``expired``. 404 ``offer_unknown`` when it is not this person's, or is gone."""
    async with request.app.state.db.sessionmaker() as session:
        offer = await session.scalar(select(AgentLinkOffer).where(AgentLinkOffer.id == offer_id, AgentLinkOffer.user_id == user.sub))
    if offer is None:
        raise Refused(404, "offer_unknown", OFFER_UNKNOWN)
    if offer.agent_name is not None:
        return {"state": "paired", "agent": {"name": offer.agent_name, "label": offer.agent_label or offer.agent_name}}
    return {"state": "expired" if offer.expires_at <= _now() else "waiting", "expires_in": max(int((offer.expires_at - _now()).total_seconds()), 0)}


async def _claim(state: Any, user_id: str, code: str | None, chosen: Any, reconnect: bool) -> dict[str, Any]:
    """Give the link waiting with ``code`` to ``user_id``: a new agent made for it, or the
    one of that name reconnected — the claim's work, for a code typed in Wysebee or a QR code
    the device scanned (:func:`start`'s ``offer``). Refusals as :func:`claim`'s; the code stays
    good after one."""
    from apps.cloud import agents

    s: Settings = state.settings

    refusal: Refused | None = None
    link_id = ""
    async with state.db.sessionmaker() as session:
        # The person's head lock first: their agents cannot change under the checks below.
        await changes.bump(session, user_id)
        now = _now()
        link = None
        if code is not None:
            link = await session.scalar(
                select(BrowserLink)
                .where(BrowserLink.code == code, BrowserLink.user_id.is_(None), BrowserLink.refused.is_(None), BrowserLink.code_expires_at > now)
                .with_for_update()
            )
        if link is None:
            await session.rollback()
            raise Refused(404, "code_unknown", CODE_UNKNOWN)
        link_id = link.id
        kind = await agent_types.by_id(session, link.agent_type_id)
        if kind is None:
            # Its type was taken off the table, or broke its limits, since it started.
            await session.rollback()
            raise Refused(422, "invalid_kind", NO_TYPE)
        # The person's own name for it, if they gave one (the start's rule), else the device's.
        label = _line(chosen, MAX_LABEL) if chosen is not None else link.name
        handle = handle_of(label) if label is not None else None
        mine = (await session.execute(select(UserAgent.agent_id, UserAgent.name).where(UserAgent.user_id == user_id))).all()
        existing = next((a for a in mine if a.name == handle), None)
        if handle is None or label is None:
            refusal = Refused(422, "invalid_name", REFUSALS["invalid_name"])
        elif handle in RESERVED:
            refusal = Refused(422, "reserved", REFUSALS["reserved"])
        elif handle == WYSEBEE or existing is not None:
            agent, may = await _name_taken(session, user_id, handle, link, kind)
            if not (reconnect and may):
                refusal = Refused(409, "name_taken", REFUSALS["name_taken"], {"agent": agent, "reconnect": may})
        elif sum(1 for a in mine if a.name != WYSEBEE) >= MAX_AGENTS:
            refusal = Refused(409, "too_many", REFUSALS["too_many"])
        if refusal is None:
            # What the deployment's policy allows (apps/cloud/extensions.py): one more device.
            said = await state.policy.may_pair(session, user_id)
            refusal = None if said is None else Refused(said.status, said.code, said.detail, said.more)
        if refusal is None:
            assert handle is not None and label is not None
            row = await agents.by_id(session, user_id, existing.agent_id) if existing is not None else None
            try:
                if row is not None:
                    # Reconnected: the agent keeps everything; it gets the link (keep_link writes
                    # its field, and a Mac's folders as the new link holds them: none).
                    label = _line((row.config or {}).get("label"), MAX_LABEL) or handle
                else:
                    # Where the link is held is its type's `link_field`, set by keep_link once the
                    # link holds the agent; what else a new agent of it starts with is its pack's
                    # (apps/cloud/agent_packs.py: the browser's Browser access). A Mac: nothing
                    # more (its folders are its to say, on its first poll).
                    pack = next(iter(state.agent_packs.by_kind.get(kind.kind, [])), None)
                    config: dict[str, Any] = {"label": label, **(pack.new_config if pack is not None else {})}
                    config.update({PAIRED_KIND: kind.key, TYPE_ID: kind.id})
                    row = await agents.save(session, s, user_id, None, name=handle, config=config)
                # The link names the agent as it is — what the device shows, which may differ
                # from what was typed there.
                link.user_id, link.agent_id, link.claimed_at, link.code, link.name = user_id, row.agent_id, now, None, label
                # Waiting no more: where it started is forgotten.
                link.start_address = None
                await session.flush()
                await agents.save(session, s, user_id, row, name=row.name, config=dict(row.config or {}), description=row.description)
                await _notify(session, [_link_key(link.id)])
                await agents._commit(session, handle)
            except HTTPException:
                # The name index caught a clash the check above could not see: an agent made
                # meanwhile, so nothing can be said of it but its name.
                await session.rollback()
                refusal = Refused(409, "name_taken", REFUSALS["name_taken"], {"agent": {"name": handle, "label": handle}, "reconnect": False})
            else:
                out = {"link": _link_out(link, now, s, kind, handle), "agent": {"name": handle, "label": label}}
        if refusal is not None:
            # The code stays good: the person may claim again with it.
            await session.rollback()
            raise refusal
    _wake(state, [_link_key(link_id)])
    logger.info("browser link paired")
    return out


# --- the extension's side ---------------------------------------------------------------------


async def _extension_link(session: AsyncSession, request: Request, link_id: str) -> BrowserLink:
    """The link this extension is, by its secret: 401 ``unpaired`` when the link is gone or
    the secret is not its own."""
    scheme, _, secret = request.headers.get("authorization", "").partition(" ")
    given = hashlib.sha256(secret.strip().encode()).hexdigest()
    link = await session.get(BrowserLink, link_id, populate_existing=True)
    matches = hmac.compare_digest(given, link.secret_hash if link is not None else "0" * 64)
    if link is None or not matches or scheme.lower() != "bearer":
        raise Refused(401, "unpaired", _said(UNPAIRED, link.kind if link is not None and matches else None))
    return link


async def _expire(session: AsyncSession, s: Settings, now: datetime, *where: Any) -> list[str]:
    """Fail the calls (of ``where``) nobody took in time, or that ran out of their own time
    — those the extension is told to stop; the keys to wake."""
    # Not reached, said of each kind's device: each pack's that says it.
    said = [(k, w[NOT_REACHED]) for k, w in agent_packs.kinds_words() if NOT_REACHED in w]
    error = case(*((BrowserCall.link_id.in_(select(BrowserLink.id).where(BrowserLink.kind == k)), sentence) for k, sentence in said), else_=NOT_REACHED)
    not_taken = (await session.execute(
        update(BrowserCall)
        .where(*where, BrowserCall.status == "pending", BrowserCall.created_at < now - timedelta(seconds=s.browser_link_claim_seconds))
        .values(status="failed", ok=False, error=error, finished_at=now)
        .returning(BrowserCall.id)
    )).scalars().all()
    late = (await session.execute(
        select(BrowserCall)
        .where(*where, BrowserCall.status == "claimed", BrowserCall.created_at + func.make_interval(0, 0, 0, 0, 0, 0, BrowserCall.timeout_seconds) < now)
        .with_for_update()
    )).scalars().all()
    kinds = dict((await session.execute(select(BrowserLink.id, BrowserLink.kind).where(BrowserLink.id.in_({c.link_id for c in late})))).all()) if late else {}
    for call in late:
        call.status, call.ok, call.error = "failed", False, _said(TIMED_OUT, kinds.get(call.link_id)).format(seconds=call.timeout_seconds)
        call.finished_at, call.cancelled = now, True
    return [_call_key(i) for i in not_taken] + [_call_key(c.id) for c in late] + [_link_key(c.link_id) for c in late]


async def _poll_once(state: Any, request: Request, link_id: str, status: dict[str, Any]) -> tuple[dict[str, Any] | Refused | None, dict[str, Any]]:
    """One look: the answer (or refusal) when there is one to give now, and what to answer
    once the wait is over when there is not."""
    s: Settings = state.settings
    now = _now()
    woke: list[str] = []
    answer: dict[str, Any] | Refused | None = None
    async with state.db.sessionmaker() as session, session.begin():
        link = await _extension_link(session, request, link_id)
        if link.user_id is None:
            if link.refused:
                answer = {"state": "refused", "reason": link.refused, "message": REFUSALS.get(link.refused, link.refused)}
            elif link.code_expires_at <= now:
                await session.delete(link)
                answer = Refused(410, "expired", EXPIRED)
                return answer, {}
            return answer, {"state": "pending", "expires_in": max(int((link.code_expires_at - now).total_seconds()), 0)}
        agent = await session.get(UserAgent, link.agent_id) if link.agent_id else None
        if agent is None:
            # Its agent is gone (deleted by a client that did not say so).
            await session.delete(link)
            return Refused(401, "unpaired", _said(UNPAIRED, link.kind)), {}
        link.last_seen = now
        if isinstance(status.get("paused"), bool):
            link.paused = status["paused"]
        link.extension_version = _short(status.get("extension_version")) or link.extension_version
        link.browser = _short(status.get("browser")) or link.browser
        # What its kind's pack reads from the poll beyond calls (the computer's: what it allowed,
        # its folders), and stores.
        pack = state.agent_packs.of_kind(link.kind)
        ctx = agent_packs.LinkContext(session, state, link, agent)
        if pack is not None:
            await pack.apply_poll(ctx, pack.read_poll(status))
        mine = BrowserCall.link_id == link.id
        woke = await _expire(session, s, now, mine)
        await session.execute(delete(BrowserCall).where(mine, BrowserCall.created_at < now - timedelta(seconds=s.browser_link_keep_seconds)))
        waiting = (
            select(BrowserCall.id).where(mine, BrowserCall.status == "pending")
            .order_by(BrowserCall.created_at).limit(CLAIM_BATCH).with_for_update(skip_locked=True).scalar_subquery()
        )
        calls = (await session.execute(
            update(BrowserCall).where(BrowserCall.id.in_(waiting)).values(status="claimed", claimed_at=now)
            .returning(BrowserCall.id, BrowserCall.op, BrowserCall.args, BrowserCall.created_at)
        )).all()
        stopped = (await session.execute(select(BrowserCall.id).where(mine, BrowserCall.cancelled.is_(True)))).scalars().all()
        if stopped:
            # Told now: a cancelled call is done with; a failed one waits for its driver.
            await session.execute(delete(BrowserCall).where(BrowserCall.id.in_(stopped), BrowserCall.status == "cancelled"))
            await session.execute(update(BrowserCall).where(BrowserCall.id.in_(stopped)).values(cancelled=False))
        woke += [_call_key(c.id) for c in calls]
        await _notify(session, woke)
        idle = {
            "state": "paired", "agent": {"name": agent.name, "label": link.name},
            "calls": [{"id": c.id, "op": c.op, "args": c.args} for c in sorted(calls, key=lambda c: c.created_at)],
            "cancelled": list(stopped),
        }
        if pack is not None:
            idle.update(await pack.poll_extra(ctx))
        if calls or stopped:
            answer = idle
    _wake(state, woke)
    return answer, idle


@links.post("/{link_id}/poll")
async def poll(request: Request, link_id: str = LinkId, wait: float = Query(default=0, ge=0)) -> dict[str, Any]:
    """The extension asks where it stands and for work: at once when there is something to
    say (claimed, refused, calls waiting or stopped), else after ``wait`` seconds (25 at
    most). Every poll of a paired link marks it connected."""
    state = request.app.state
    s: Settings = state.settings
    hub: RelayHub = state.relay_hub
    body = await _body(request, POLL_BODY)
    status = dict(body["status"]) if isinstance(body.get("status"), dict) else {}
    deadline = time.monotonic() + min(wait, s.browser_link_poll_seconds)
    was_pending = False
    seen: Any = None
    with hub.subscribe(_link_key(link_id)) as woken:
        while True:
            woken.clear()
            answer, idle = await _poll_once(state, request, link_id, status)
            # `paused` is the extension's switch as it was when this poll was sent: a long poll
            # still waiting must not undo the one sent since, switched off (2026-11-14).
            status.pop("paused", None)
            if isinstance(answer, Refused):
                raise answer
            if answer is not None:
                return answer
            remaining = deadline - time.monotonic()
            # Claimed while this poll waited, or (a Mac) its agent's programs or skills changed:
            # said at once.
            now_seen = (idle.get("programs"), idle.get("skills"))
            changed = seen is not None and now_seen != seen
            if remaining <= 0 or (was_pending and idle["state"] == "paired") or changed or await request.is_disconnected():
                return idle
            was_pending = idle["state"] == "pending"
            seen = now_seen
            if idle["state"] == "pending":
                # Not claimed yet: look again when the code runs out, at the latest.
                remaining = min(remaining, idle["expires_in"] + 1)
            await _wait(woken, remaining if hub.listening else min(remaining, RECHECK_SECONDS))


@links.post("/{link_id}/calls/{call_id}/result", status_code=204)
async def post_result(request: Request, link_id: str = LinkId, call_id: str = Path(pattern=ID)) -> Response:
    """The extension's answer to a call it took. One for a call that is gone (cancelled,
    out of time, expired), or that this link never took, is a 404, and dropped — said before
    the body is read (2026-11-20), so only a call waiting for its answer costs reading up to
    ``browser_link_result_max_bytes``."""
    state = request.app.state
    s: Settings = state.settings
    async with state.db.sessionmaker() as session:
        link = await _extension_link(session, request, link_id)
        kind = link.kind
        taken = link.user_id is not None and await session.scalar(
            select(BrowserCall.id).where(BrowserCall.id == call_id, BrowserCall.link_id == link_id, BrowserCall.status == "claimed")
        ) is not None
    if not taken:
        raise Refused(404, "call_gone", CALL_GONE)
    values: dict[str, Any]
    refused: Refused | None = None
    try:
        body = await _body(request, s.browser_link_result_max_bytes)
    except Refused as too_big:
        # Said to the driver too, rather than leave it waiting for its timeout.
        values, refused = {"status": "failed", "ok": False, "error": TOO_BIG}, too_big
    else:
        if body.get("ok") is True:
            values = {"status": "done", "ok": True, "result": _no_nul(body.get("result"))}
        else:
            error = body.get("error")
            values = {"status": "done", "ok": False, "error": (error if isinstance(error, str) and error else _said(NO_ANSWER, kind)).replace("\x00", "")[:20_000]}
    keys = [_call_key(call_id)]
    async with state.db.sessionmaker() as session, session.begin():
        owner = (await _extension_link(session, request, link_id)).user_id
        done = (await session.execute(
            update(BrowserCall)
            .where(BrowserCall.id == call_id, BrowserCall.link_id == link_id, BrowserCall.status == "claimed")
            .values(**values, finished_at=_now())
            .returning(BrowserCall.id)
        )).scalar_one_or_none()
        if done is not None:
            # The result came through the relay from the device (apps/cloud/traffic.py).
            await traffic.add(session, owner, getattr(request.state, "body_bytes", 0))
            await _notify(session, keys)
    if done is None:
        raise Refused(404, "call_gone", CALL_GONE)
    _wake(state, keys)
    if refused is not None:
        raise refused
    return Response(status_code=204)


@links.get("/{link_id}/skill-files/{file_id}")
async def skill_file(request: Request, link_id: str = LinkId, file_id: str = Path()) -> Response:
    """A file of one of the agent's skills, for Wysebee Agent for Mac (by its secret): the
    bytes, only when the link is an ``osx`` one, paired, and the file is in the synced list of
    a skill its agent has — else 404 ``file_unknown``, whoever else holds it."""
    state = request.app.state
    store: AssetStore = state.skill_file_store
    async with state.db.sessionmaker() as session:
        link = await _extension_link(session, request, link_id)
        pack = state.agent_packs.of_kind(link.kind)
        serves = pack is not None and pack.serves_skill_files and link.user_id is not None
        ids = {f["id"] for s in await _skills(session, link.user_id or "", link.agent_id) for f in s["files"]} if serves else set()
        held = file_id in ids and await session.get(CloudSkillFile, (link.user_id, file_id)) is not None
    # The person's own file store, as GET /skill-files/{id} reads it.
    data = await store.read(link.user_id or "", file_id) if held else None
    if data is None:
        raise Refused(404, "file_unknown", FILE_UNKNOWN)
    await traffic.add_now(state, link.user_id, len(data))
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, max-age=31536000, immutable", "Content-Disposition": "attachment", "X-Content-Type-Options": "nosniff"},
    )


@links.delete("/{link_id}", status_code=204)
async def unpair_from_extension(request: Request, link_id: str = LinkId) -> Response:
    """The extension unpairs itself. Its agent stays, without a browser."""
    state = request.app.state
    async with state.db.sessionmaker() as session:
        link = await _extension_link(session, request, link_id)
        await _unpair(session, state.settings, link)
    _wake(state, [_link_key(link_id)])
    return Response(status_code=204)


async def _unpair(session: AsyncSession, settings: Settings, link: BrowserLink) -> None:
    """Delete the link (its calls with it) and take it out of its agent's settings."""
    await session.execute(delete(BrowserLink).where(BrowserLink.id == link.id))
    if link.user_id is not None:
        await _forget_link(session, settings, link.user_id, link.agent_id)
    await _notify(session, [_link_key(link.id)])
    await session.commit()


# --- managing links (the person) ---------------------------------------------------------------


async def _own_link(session: AsyncSession, user_id: str, link_id: str, agent: str | None = None) -> BrowserLink:
    """The account's own paired link (and, with ``agent``, an agent's id, that agent's own), or
    404."""
    link = await session.get(BrowserLink, link_id, populate_existing=True)
    if link is None or link.user_id != user_id or (agent is not None and link.agent_id != agent):
        raise Refused(404, "browser_unpaired", NO_LINK)
    return link


@links.get("")
@links.get("/", include_in_schema=False)
async def list_links(request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
    """The account's paired browsers."""
    state = request.app.state
    async with state.db.sessionmaker() as session:
        links = (await session.scalars(select(BrowserLink).where(BrowserLink.user_id == user.sub).order_by(BrowserLink.created_at))).all()
        types = {t.id: t for t in await agent_types.all_types(session)}
        names = dict((await session.execute(select(UserAgent.agent_id, UserAgent.name).where(UserAgent.user_id == user.sub))).all())
    now = _now()
    return {"links": [_link_out(link, now, state.settings, types.get(str(link.agent_type_id)), names.get(link.agent_id)) for link in links]}


@links.post("/{link_id}/unpair", status_code=204)
async def unpair(request: Request, link_id: str = LinkId, user: User = Depends(current_user)) -> Response:
    """Unpair one of the account's browsers: the extension's next poll is a 401, and its
    agent stays, without a browser."""
    state = request.app.state
    async with state.db.sessionmaker() as session:
        link = await _own_link(session, user.sub, link_id)
        await _unpair(session, state.settings, link)
    _wake(state, [_link_key(link_id)])
    return Response(status_code=204)


# --- driving the browser ------------------------------------------------------------------------


def _call_out(call: BrowserCall) -> dict[str, Any]:
    if call.status == "done":
        out: dict[str, Any] = {"id": call.id, "status": "done", "ok": bool(call.ok)}
        out.update({"result": call.result} if call.ok else {"error": call.error or ""})
        return out
    if call.status == "failed":
        return {"id": call.id, "status": "failed", "ok": False, "error": call.error or ""}
    return {"id": call.id, "status": "waiting" if call.status == "pending" else "running"}


async def start_call(state: Any, user_id: str, link_id: str, op: Any, args: Any, timeout_seconds: Any = None, *, agent: str | None = None) -> str:
    """Queue one op for the account's link; its id. :class:`Refused` when it cannot be:
    not an op, no such link for this account (or not ``agent``'s), the browser offline, or
    refused by the deployment's policy (apps/cloud/extensions.py)."""
    s: Settings = state.settings
    if not isinstance(op, str):
        raise Refused(422, "invalid_op", NOT_AN_OP)
    args = {} if args is None else args
    if not isinstance(args, dict):
        raise Refused(422, "invalid_args", "'args' must be an object.")
    if len(json.dumps(args)) > s.browser_link_args_max_bytes:
        raise Refused(413, "too_big", f"The arguments are too long: at most {s.browser_link_args_max_bytes} characters.")
    timeout = s.browser_link_call_seconds if timeout_seconds is None else timeout_seconds
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not s.browser_link_call_min_seconds <= timeout <= s.browser_link_call_max_seconds:
        raise Refused(422, "invalid_timeout", f"'timeout_seconds' is {s.browser_link_call_min_seconds:.0f} to {s.browser_link_call_max_seconds:.0f}.")
    now = _now()
    call_id = uuid.uuid4().hex
    async with state.db.sessionmaker() as session, session.begin():
        link = await _own_link(session, user_id, link_id, agent)
        kind = await agent_types.by_id(session, link.agent_type_id)
        if kind is None or op not in kind.ops:
            # Not an op its type lists (a browser's asked of a Mac, or a Mac's of a browser),
            # or its type is no longer one: the cloud relays only what the row says.
            raise Refused(422, "invalid_op", _said(NOT_AN_OP, link.kind))
        # Switched off in the extension (its last poll said `paused`, 2026-11-14): refused at
        # once, not left for a browser that has stopped polling to never take.
        if link.paused or not _connected(link, now, s):
            raise Refused(409, "browser_offline", _said(OFFLINE, link.kind))
        # What the deployment's policy allows (apps/cloud/extensions.py): this call, now.
        said = await state.policy.may_call(session, user_id, link.id, now)
        if said is not None:
            raise Refused(said.status, said.code, said.detail, said.more)
        session.add(BrowserCall(id=call_id, link_id=link.id, user_id=user_id, op=op, args=_no_nul(args), status="pending", timeout_seconds=float(timeout), created_at=now))
        # The arguments go through the relay to the device (apps/cloud/traffic.py).
        await traffic.add(session, user_id, len(json.dumps(args).encode()), now)
        await _notify(session, [_link_key(link.id)])
    _wake(state, [_link_key(link_id)])
    return call_id


async def _check_call(state: Any, user_id: str, call_id: str) -> tuple[dict[str, Any], float | None]:
    """The call as it stands — read once finished, and then forgotten — and, while it is
    not, how long until it runs out of time by itself."""
    s: Settings = state.settings
    now = _now()
    async with state.db.sessionmaker() as session, session.begin():
        woke = await _expire(session, s, now, BrowserCall.id == call_id, BrowserCall.user_id == user_id)
        call = await session.scalar(select(BrowserCall).where(BrowserCall.id == call_id, BrowserCall.user_id == user_id))
        if call is None or call.status == "cancelled":
            raise Refused(404, "call_gone", CALL_GONE)
        out = _call_out(call)
        left: float | None = None
        if call.status in ("done", "failed"):
            if call.cancelled:
                # Out of time, and its extension has yet to be told to stop: kept for that.
                call.status, call.result = "cancelled", None
            else:
                await session.delete(call)
        else:
            limit = s.browser_link_claim_seconds if call.status == "pending" else call.timeout_seconds
            left = max((call.created_at + timedelta(seconds=limit) - now).total_seconds(), 0)
        await _notify(session, woke)
    _wake(state, woke)
    return out, left


async def wait_call(state: Any, user_id: str, call_id: str, wait: float, disconnected: Any = None) -> dict[str, Any]:
    """The call, once it is done or has failed, else as it stands after ``wait`` seconds."""
    s: Settings = state.settings
    hub: RelayHub = state.relay_hub
    deadline = time.monotonic() + min(max(wait, 0), s.browser_link_wait_seconds)
    with hub.subscribe(_call_key(call_id)) as woken:
        while True:
            woken.clear()
            out, left = await _check_call(state, user_id, call_id)
            remaining = deadline - time.monotonic()
            if left is None or remaining <= 0 or (disconnected is not None and await disconnected()):
                return out
            remaining = min(remaining, left + 0.05)
            await _wait(woken, remaining if hub.listening else min(remaining, RECHECK_SECONDS))


async def cancel_call(state: Any, user_id: str, call_id: str) -> None:
    """Stop a call: one the extension has is cancelled there on its next poll; any other
    is simply forgotten."""
    keys: list[str] = []
    async with state.db.sessionmaker() as session, session.begin():
        call = await session.scalar(select(BrowserCall).where(BrowserCall.id == call_id, BrowserCall.user_id == user_id).with_for_update())
        if call is None:
            return
        keys = [_call_key(call.id), _link_key(call.link_id)]
        if call.status == "claimed":
            call.status, call.cancelled, call.finished_at = "cancelled", True, _now()
        elif call.cancelled:
            # Failed for running out of time, the extension not told yet: kept for that.
            call.status, call.result = "cancelled", None
        else:
            await session.delete(call)
        await _notify(session, keys)
    _wake(state, keys)


async def run_op(state: Any, user_id: str, link_id: str, op: str, args: dict[str, Any], *, agent: str | None = None, timeout_seconds: float | None = None) -> dict[str, Any]:
    """Run one op on a link and wait for its result, in process — for this server's own
    agents' tools (apps/cloud/agent_chat/toolbox.py), through the same queue as any driver,
    no HTTP. ``agent``: the agent asking, refused unless the link is its own.

    ``{"ok": True, "result"}``, or ``{"ok": False, "error", "unavailable"}`` —
    ``unavailable``: the browser was never reached (unpaired, offline, did not take it),
    rather than refusing or failing the op. Cancelled while waiting, it stops the call."""
    try:
        call_id = await start_call(state, user_id, link_id, op, args, timeout_seconds, agent=agent)
    except Refused as r:
        return {"ok": False, "error": r.detail, "unavailable": r.code in ("browser_unpaired", "browser_offline")}
    try:
        while True:
            out = await wait_call(state, user_id, call_id, state.settings.browser_link_wait_seconds)
            if out["status"] in ("done", "failed"):
                break
    except Refused as r:
        return {"ok": False, "error": r.detail, "unavailable": False}
    except asyncio.CancelledError:
        await asyncio.shield(cancel_call(state, user_id, call_id))
        raise
    if out["ok"]:
        return {"ok": True, "result": out.get("result")}
    return {"ok": False, "error": out["error"], "unavailable": out["status"] == "failed" and out["error"] in {NOT_REACHED, *(w[NOT_REACHED] for w in agent_packs.all_words() if NOT_REACHED in w)}}


@links.post("/{link_id}/calls")
async def call_browser(request: Request, link_id: str = LinkId, wait: float = Query(default=0, ge=0), user: User = Depends(current_user)) -> dict[str, Any]:
    """Ask the browser for one op: answers when it is done, or after ``wait`` seconds with
    how it stands (then ask ``GET /calls/{id}``)."""
    state = request.app.state
    body = await _body(request, state.settings.browser_link_args_max_bytes + SMALL_BODY)
    call_id = await start_call(state, user.sub, link_id, body.get("op"), body.get("args"), body.get("timeout_seconds"))
    return await wait_call(state, user.sub, call_id, wait, request.is_disconnected)


@links.get("/calls/{call_id}")
async def get_call(request: Request, call_id: str = Path(pattern=ID), wait: float = Query(default=0, ge=0), user: User = Depends(current_user)) -> dict[str, Any]:
    """The same answer: ask until ``done`` or ``failed``. A finished call is forgotten once read."""
    return await wait_call(request.app.state, user.sub, call_id, wait, request.is_disconnected)


@links.post("/calls/{call_id}/cancel", status_code=204)
async def cancel(request: Request, call_id: str = Path(pattern=ID), user: User = Depends(current_user)) -> Response:
    """Stop a call; the extension is told on its next poll."""
    await cancel_call(request.app.state, user.sub, call_id)
    return Response(status_code=204)


# Every route under each prefix: `/api/v1/browser-links` (the extension, and older clients)
# and `/api/v1/agent-links`. A link of either kind is reached at either.
router = APIRouter()
router.include_router(links, prefix=PREFIXES[0], tags=["Browser link"])
router.include_router(links, prefix=PREFIXES[1], tags=["Agent link"])
