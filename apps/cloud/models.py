"""ORM models. Every table here has an Alembic migration under migrations/versions/."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from apps.cloud.db import Base


class UserTask(Base):
    """One of a person's tasks (apps/cloud/tasks.py, migration 0065): a plan, its steps and
    when it runs, which Wysebee Cloud's scheduler carries out (apps/cloud/task_runner.py).
    ``task_id`` is what everything links by — its schedule and its runs, deleted with it — so
    a task can be renamed; ``name`` is unique per owner. ``config`` holds ``prompt`` (the
    plan), ``description``, ``label``, the browser settings, ``trigger`` and ``steps``
    (``[{agent_id, tool, arguments}]``). (Until 0065 a task was a ``task`` cloud item, its
    settings JSON text in ``data.CONFIG``; cloud items went with it.)"""

    __tablename__ = "user_tasks"
    __table_args__ = (Index("ix_user_tasks_user_name", "user_id", "name", unique=True),)

    task_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(200))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # Who made it and who changed it last (user ids, migration 0066; apps/cloud/people.py).
    created_by: Mapped[str] = mapped_column(String(255))
    updated_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CloudConfigHead(Base):
    """A person's change counter: saving one of their agents or tasks bumps it, in the same
    transaction (taking this row's lock), and the change feed (apps/cloud/config_events.py)
    tells their open pages. (``purged_through`` was the cloud items' tombstones' mark; unused
    since they went with migration 0065.)"""

    __tablename__ = "cloud_config_heads"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger)
    purged_through: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CloudSkillFile(Base):
    """One file of one of a person's skills (apps/cloud/skill_files.py),
    content-addressed: ``file_id`` is the lowercase hex SHA-256 of the bytes, which are kept
    by the skill file store (an asset store of their own), not in this table."""

    __tablename__ = "cloud_skill_files"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    file_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserAgent(Base):
    """One of a person's agents (apps/cloud/agents.py, migration 0063). ``agent_id`` is what
    everything links by — its skills, tools, paired device, tasks' steps, runs, notifications —
    so the agent can be renamed; ``name`` is its @handle, unique per owner. ``config`` is its
    settings (label, instructions, avatar, which device it is paired with…). Wysebee's own
    agent is a row too, named ``wysebee``, made the first time it is needed. (Until 0063 an
    agent was an ``agent`` cloud item, its settings JSON text in ``data.CONFIG``.)"""

    __tablename__ = "user_agents"
    __table_args__ = (Index("ix_user_agents_user_name", "user_id", "name", unique=True),)

    agent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(30))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Who made it and who changed it last (user ids, migration 0066; apps/cloud/people.py).
    created_by: Mapped[str] = mapped_column(String(255))
    updated_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserSkill(Base):
    """One of a person's skills (apps/cloud/skills.py): a folder — ``SKILL.md`` and the files
    it needs — whose bytes are in the skill file store (:class:`CloudSkillFile`). ``skill_id``
    is what everything links by, so the skill can be renamed; ``name`` is unique per owner.
    (Until migration 0060 a skill was a ``skill`` cloud item, keyed and linked by its name.)"""

    __tablename__ = "user_skills"
    __table_args__ = (Index("ix_user_skills_user_name", "user_id", "name", unique=True),)

    skill_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    # The owner: whose skill file store holds its files.
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    # [{path, id, size}]: each file's path in the folder, its id in the store, its size.
    files: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # The folders the person made that hold no file yet (migration 0067): a folder with files
    # is in their paths. Never sent to a Mac.
    folders: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # Who made it and who changed it last (user ids, migration 0066; apps/cloud/people.py).
    created_by: Mapped[str] = mapped_column(String(255))
    updated_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SkillChange(Base):
    """A change to a skill proposed by the person's AI app (apps/cloud/skill_changes.py,
    migration 0075): a file's whole text, or a folder, kept here — not in the skill — until the
    person applies or discards it. One per skill and path; deleted with the skill."""

    __tablename__ = "skill_changes"
    __table_args__ = (Index("ix_skill_changes_skill_path", "skill_id", "path", unique=True),)

    change_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    skill_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("user_skills.skill_id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String(1024))
    folder: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentSkill(Base):
    """A skill an agent has (apps/cloud/skills.py), in the agent's order. ``user_id`` is the
    agent's owner; agent and skill are linked by id, so renaming either keeps it, and deleting
    either deletes the link. The skill's owner is its own row's — today always the agent's owner;
    another person's, once skills can be shared. Deleting the skill deletes the link."""

    __tablename__ = "agent_skills"
    __table_args__ = (Index("ix_agent_skills_skill", "skill_id"),)

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    agent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("user_agents.agent_id", ondelete="CASCADE"), primary_key=True)
    skill_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("user_skills.skill_id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SkillRequest(Base):
    """One of the person's skills their AI app asks to give one of their agents
    (apps/cloud/skill_changes.py, migration 0076): nothing until the person gives it (an
    :class:`AgentSkill`) or declines it. Deleted with the agent or the skill."""

    __tablename__ = "skill_requests"

    user_id: Mapped[str] = mapped_column(String(255), index=True)
    agent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("user_agents.agent_id", ondelete="CASCADE"), primary_key=True)
    skill_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("user_skills.skill_id", ondelete="CASCADE"), primary_key=True)
    reason: Mapped[str] = mapped_column(Text, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MenuOrder(Base):
    """The order a person dragged a menu's items into (apps/cloud/menu_orders.py, migration
    0087): one row per menu (``workspaces``; ``tasks`` and ``agents`` later) and scope — the
    list the items are in — its items' keys in order. Items not in it follow, by name."""

    __tablename__ = "menu_orders"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    menu: Mapped[str] = mapped_column(String(40), primary_key=True)
    scope: Mapped[str] = mapped_column(String(500), primary_key=True)
    keys: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserWorkspace(Base):
    """One of a person's workspaces (apps/cloud/workspaces.py): a folder of documentation files
    in the workspace store (``user-workspaces`` in production), ``<user id>/<workspace id>/``
    (migration 0073; until then, projects).
    The files themselves are not rows: the store's own listing is the folder structure.
    Everything links by ``workspace_id``, so a workspace can be renamed."""

    __tablename__ = "user_workspaces"
    __table_args__ = (Index("ix_user_workspaces_user_name", "user_id", text("lower(name)"), unique=True),)

    workspace_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    # The workspace's folder in the store, unique per person whatever its case.
    name: Mapped[str] = mapped_column(String(80))
    # Where the folder is kept: the blob container's URL and the folder's prefix.
    resource_url: Mapped[str] = mapped_column(Text)
    # Who made it and who changed it last (user ids, migration 0066; apps/cloud/people.py).
    created_by: Mapped[str] = mapped_column(String(255))
    updated_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())



class WorkspacePage(Base):
    """A rich HTML page the person's AI app made from one workspace document
    (apps/cloud/workspace_pages.py, migration 0077): its bytes in the workspace store at
    ``<workspace>/.pages/<page id>.html``. One per document; deleted with the workspace."""

    __tablename__ = "workspace_pages"
    __table_args__ = (Index("ix_workspace_pages_workspace_source", "workspace_id", "source_path", unique=True),)

    page_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    workspace_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("user_workspaces.workspace_id", ondelete="CASCADE"))
    source_path: Mapped[str] = mapped_column(String(400))
    title: Mapped[str] = mapped_column(String(200), server_default=text("''"))
    # The secret part of the address it is shown at (``/api/v1/page-view/<id>/<key>``), new each write.
    view_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Shared (migration 0084): with whom — "public", or None for only its owner — and the secret
    # of its public address (``/s/<key>``), kept across rewrites, gone when sharing stops.
    share_audience: Mapped[str | None] = mapped_column(String(20), nullable=True)
    share_key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Made directly (migration 0085): "file" when the page is the ``.html`` file at
    # ``source_path`` itself, "view" when it is kept in ``.pages`` for that document.
    kind: Mapped[str] = mapped_column(String(10), server_default=text("'view'"))


class PageToolGrant(Base):
    """A tool a page may run without asking its owner again (apps/cloud/page_tools.py,
    migration 0086): ``reads`` when it only reads; one that can change something keeps the
    page's ``view_key`` it was allowed for, and holds only while the page is unchanged."""

    __tablename__ = "page_tool_grants"

    page_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("workspace_pages.page_id", ondelete="CASCADE"), primary_key=True)
    agent: Mapped[str] = mapped_column(String(100), primary_key=True)
    tool: Mapped[str] = mapped_column(String(200), primary_key=True)
    reads: Mapped[bool] = mapped_column(Boolean)
    view_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class WorkspaceAssistant(Base):
    """Wysebee Assistant on the page open in a person's editor (apps/cloud/workspace_assistant.py):
    one at a time per person — the page open, its text as the editor holds it (saved or not)
    and a revision bumped by every write, the person's or their AI app's."""

    __tablename__ = "workspace_assistants"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    # The page's workspace — or, since migration 0067, its skill (apps/cloud/skill_folders.py):
    # exactly one of the two.
    workspace_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), ForeignKey("user_workspaces.workspace_id", ondelete="CASCADE"), nullable=True)
    skill_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), ForeignKey("user_skills.skill_id", ondelete="CASCADE"), nullable=True)
    # Who opened it: "person" (their editor) or "ai" (their AI app made a skill: create_skill),
    # so an editor open on another page goes to it.
    opened_by: Mapped[str] = mapped_column(String(8), server_default=text("'person'"))
    # The page's path inside the workspace.
    path: Mapped[str] = mapped_column(String(400))
    content: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(BigInteger)
    # Who wrote the text last: "person" or "ai".
    writer: Mapped[str] = mapped_column(String(16))
    # The AI app, as it names itself when it connects, and when it was last heard from.
    ai_client: Mapped[str | None] = mapped_column(String(100))
    ai_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the editor was last heard from: one not heard from for a while is closed.
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserNotification(Base):
    """A notification the person's AI app sent through Wysebee Assistant's ``send_notification``
    (apps/cloud/notifications.py), shown on the website's Activity page, newest first. Unread
    until the person opens Activity."""

    __tablename__ = "user_notifications"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    # The agent it is from (by handle from migration 0056, by id from 0063): the one whose
    # send_notification sent it, or a due task's first step's; none for one sent before agents
    # sent them, or whose agent was deleted.
    agent_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), ForeignKey("user_agents.agent_id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_user_notifications_user", "user_id", "created_at"),)


class TaskSchedule(Base):
    """When one of a person's scheduled tasks is next due (apps/cloud/task_runner.py), kept
    from its trigger on every save; ``running_since`` while the cloud's scheduler runs it."""

    __tablename__ = "task_schedules"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    task_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("user_tasks.task_id", ondelete="CASCADE"), primary_key=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    running_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_task_schedules_due", "next_run_at"),)


class McpHiddenAgent(Base):
    """One of a person's agents they turned off for AI apps in its Connect AI app tab
    (apps/cloud/agent_mcp.py): Wysebee Assistant neither lists it nor runs its tools. Kept
    here, not in the agent's settings, which the website rebuilds on every save. By id (from
    migration 0063): renaming the agent keeps it off; deleting it deletes this."""

    __tablename__ = "mcp_hidden_agents"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    agent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("user_agents.agent_id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CloudAsset(Base):
    """One file a person stored here (apps/cloud/assets.py) — Wysebee's avatar image is
    the first. Content-addressed: ``asset_id`` is the lowercase hex SHA-256 of the bytes,
    which are kept by the asset store (apps/cloud/asset_storage.py), not in this table."""

    __tablename__ = "cloud_assets"

    # The Auth0 `sub`: assets are private to their owner.
    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # As the upload declared it (one of assets.CONTENT_TYPES); served back with the bytes.
    content_type: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- A person's Wysebee data (apps/cloud/userdata.py) ------------------------------------
#
# The person's Wysebee data (apps/cloud/userdata.py), each table partitioned by the person
# (`user_id`, the Auth0 `sub`, first in every key). Columns were the old desktop's, in lower
# case; times are ISO text, JSON columns JSONB.


class UserTopic(Base):
    """A topic in Home's menu (desktop `config_TOPICS`)."""

    __tablename__ = "user_topics"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    created_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ux_user_topics_name", "user_id", text("lower(name)"), unique=True),)


class UserConversation(Base):
    """Home's Chat or a topic's whole history (desktop `user_data_CONVERSATIONS`)."""

    __tablename__ = "user_conversations"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    conversation_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    messages: Mapped[list[Any]] = mapped_column(JSONB)
    updated_at: Mapped[str | None] = mapped_column(Text)


class UserTaskRun(Base):
    """One run of a task (apps/cloud/task_runner.py). ``agent_id`` is its first step's agent
    (by name until migration 0063), none once that agent is deleted."""

    __tablename__ = "user_task_runs"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("user_tasks.task_id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(32))
    trigger: Mapped[str | None] = mapped_column(String(32))
    agent_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), ForeignKey("user_agents.agent_id", ondelete="SET NULL"), nullable=True)
    model: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    messages: Mapped[Any | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    recording: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_user_task_runs_task", "user_id", "task_id", "started_at"),)


class UserReminder(Base):
    """A reminder Wysebee set (desktop `config_REMINDERS`)."""

    __tablename__ = "user_reminders"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    due_at: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_user_reminders_due", "user_id", "delivered_at", "due_at"),)


class UserSetting(Base):
    """The app's settings that follow the person (desktop `config_SYSTEM_SETTINGS`, the
    keys that are not one computer's own)."""

    __tablename__ = "user_settings"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    settings_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[str | None] = mapped_column(Text)


class UserNavOrder(Base):
    """An order the person dragged a list into (desktop `config_NAV_ORDER`)."""

    __tablename__ = "user_nav_order"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    config: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[str | None] = mapped_column(Text)


class AdminUser(Base):
    """Someone who may sign in to Wysebee Admin (the wysebee-admin repository), which reads
    and changes this database directly. Nothing in this service reads it."""

    __tablename__ = "admin_users"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    password_hash: Mapped[str] = mapped_column(Text)
    is_super: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserProfile(Base):
    """One person (apps/cloud/users.py): what their app last said about them, the profile
    they chose — ``display_name`` and ``avatar`` (an emoji, or ``asset:<id>`` in the asset
    store)."""

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), server_default="")
    name: Mapped[str] = mapped_column(String(200), server_default="")
    display_name: Mapped[str] = mapped_column(String(80), server_default="")
    avatar: Mapped[str] = mapped_column(String(200), server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserTool(Base):
    """One of a person's tools (apps/cloud/user_tools.py, migration 0061): a PostgreSQL or
    Snowflake connection, a site link (Gmail, Calendar, Travel, News, Website) or a Mac
    program, given to agents through :class:`AgentTool`. ``tool_id`` is what everything links
    by, so a tool can be renamed; ``name`` is unique per owner and kind (a site link's is its
    address). ``config`` holds what is not secret; a connection's password, token or key is
    envelope-encrypted here, write-only (no route returns it), bound to ``<kind>:<tool_id>`` —
    or, for one copied from before 0061 and not saved since, to ``secret_aad``, the binding it
    was sealed with. (Until 0061 connections were the ``cloud_postgres`` and
    ``cloud_snowflake`` tables, keyed by agent name, and links and programs agents' settings.)"""

    __tablename__ = "user_tools"
    __table_args__ = (Index("ix_user_tools_user_kind_name", "user_id", "kind", "name", unique=True),)

    tool_id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(2048))
    description: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    wrapped_key: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    kek_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    secret_aad: Mapped[str | None] = mapped_column(String(600), nullable=True)
    # Who made it and who changed it last (user ids, migration 0066; apps/cloud/people.py).
    created_by: Mapped[str] = mapped_column(String(255))
    updated_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentTool(Base):
    """A tool an agent has (apps/cloud/user_tools.py), in the agent's order. ``user_id`` is the
    agent's owner; agent and tool are linked by id. Its owner is the tool's own row's — today
    always the agent's owner. Deleting either deletes the link."""

    __tablename__ = "agent_tools"
    __table_args__ = (Index("ix_agent_tools_tool", "tool_id"),)

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    agent_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("user_agents.agent_id", ondelete="CASCADE"), primary_key=True)
    tool_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("user_tools.tool_id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiTurnCount(Base):
    """How many tool calls answered through Wysebee Assistant (model calls until 2026-11-14;
    credits until 2026-11-21, when plans stopped being priced by them) a person had in one
    calendar month (UTC, ``YYYY-MM``) (apps/cloud/tool_calls.py). A new month is a new
    row, so the count starts over at each month's start; earlier months stay as history."""

    __tablename__ = "ai_turn_counts"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    count: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RelayTraffic(Base):
    """How many bytes passed through Wysebee Cloud between a person's AI app and their devices
    in one calendar month (UTC, ``YYYY-MM``) — calls' arguments and results, skill files,
    recordings (apps/cloud/traffic.py, migration 0083).
    A new month is a new row, so the count starts over."""

    __tablename__ = "relay_traffic"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentType(Base):
    """One kind of device that may pair (2026-11-13, the owner's call: a table, so a new one
    is registered from Wysebee Admin rather than built in; apps/cloud/agent_types.py). This
    service only reads it."""

    __tablename__ = "agent_types"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    # What a device sends as `kind`: `wysebee-` and lowercase a-z0-9-, 3-64 characters.
    key: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, server_default=text("''"))
    # The agent setting holding a link of this type: `browser_link`, `mac_link` or `android_link`.
    link_field: Mapped[str] = mapped_column(Text)
    ops: Mapped[list[Any]] = mapped_column(JSONB)
    tool_kinds: Mapped[list[Any]] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BrowserLink(Base):
    """One paired Wysebee Agent browser extension — one browser profile
    (apps/cloud/browser_links.py). Made by the extension with a pairing code and nobody's
    (``user_id`` null) until the person types the code in Wysebee, which creates the one
    agent that drives it (``agent_id``; deleting the agent deletes the link). Only the
    SHA-256 of the extension's secret is kept."""

    __tablename__ = "browser_links"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # Null until claimed.
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The pairing code (8 characters, shown XXXX-XXXX), gone once used.
    code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    code_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Lowercase hex SHA-256 of the extension's secret.
    secret_hash: Mapped[str] = mapped_column(String(64))
    # What the person typed in the extension: the agent's label.
    name: Mapped[str] = mapped_column(String(60))
    # The agent made for it (by handle until migration 0063).
    agent_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), ForeignKey("user_agents.agent_id", ondelete="CASCADE"), nullable=True)
    browser: Mapped[str] = mapped_column(String(32), server_default=text("''"))
    extension_version: Mapped[str] = mapped_column(String(32), server_default=text("''"))
    # The extension's own switch, as it last said.
    paused: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Why the claim was refused (name_taken, reserved, too_many, invalid_name), for the
    # extension to say.
    refused: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The extension's last poll once paired.
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Which app the link is: ``browser`` (the Wysebee Agent browser extension) or ``osx``
    # (Wysebee Agent for Mac, which runs the agent's local programs) — by its type's
    # ``link_field``, kept for readers from before agent types (2026-11-13).
    kind: Mapped[str] = mapped_column(String(16), server_default=text("'browser'"))
    # Its agent type (``agent_types.id``, migration 0050): what it may do, and its name.
    agent_type_id: Mapped[str] = mapped_column(UUID(as_uuid=False), ForeignKey("agent_types.id"))
    # osx: the names of the agent's programs that Mac has allowed, as it last said.
    approved: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # osx: the names of the agent's skills that Mac has allowed (their scripts run there), as
    # it last said.
    approved_skills: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # osx: the folders the person picked on that Mac for its agent (Local Folder), as it last
    # said — ``[{id, name, path, mode, description}]``, written into the agent's settings as
    # ``mac_folders``.
    folders: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # While waiting for its code: the SHA-256 of the address that started it, for the cap on
    # the codes one address may hold (migration 0080); cleared once claimed.
    start_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_browser_links_code", "code", unique=True),
        Index("ix_browser_links_user", "user_id"),
        Index("ix_browser_links_start_address", "start_address"),
    )


class AgentLinkOffer(Base):
    """A pairing offered by the person in Wysebee, shown there as a QR code (2026-11-17,
    apps/cloud/browser_links.py ``offers``): a device that scans it starts pairing with its
    token and is claimed for this person at once, with no code to type. One use, for ten
    minutes; only the token's SHA-256 is kept."""

    __tablename__ = "agent_link_offers"
    __table_args__ = (Index("ix_agent_link_offers_token_hash", "token_hash", unique=True),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Set while a device is pairing with it, and for good once it paired.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The agent it paired, for Wysebee to say.
    agent_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_label: Mapped[str | None] = mapped_column(String(60), nullable=True)


class BrowserRecording(Base):
    """One scenario video the Wysebee Agent extension filmed (``browser_record``) and uploaded
    (apps/cloud/browser_recordings.py). The WebM itself is in the recording store, named
    ``<user_id>/<id>.webm``; this row says what it is. Kept after the link is unpaired
    (``link_id`` names it, no foreign key) and after its agent is deleted (``agent_id``
    cleared); deleted with the account, or by the person."""

    __tablename__ = "browser_recordings"
    __table_args__ = (Index("ix_browser_recordings_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255))
    link_id: Mapped[str] = mapped_column(String(32))
    agent_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), ForeignKey("user_agents.agent_id", ondelete="SET NULL"), nullable=True)
    # What the agent called the scenario.
    name: Mapped[str] = mapped_column(String(120))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(BigInteger)
    # The site the recording began on, and the browser call that stopped it (null when it
    # ended by itself), as the extension said.
    origin: Mapped[str | None] = mapped_column(String(255), nullable=True)
    call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BrowserCall(Base):
    """One browser op for a paired extension, waiting for (or answered by) it. Kept until
    its result is read, an hour at most: its arguments and result are the person's pages."""

    __tablename__ = "browser_calls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    link_id: Mapped[str] = mapped_column(String(32), ForeignKey("browser_links.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(String(255))
    op: Mapped[str] = mapped_column(String(64))
    args: Mapped[Any] = mapped_column(JSONB)
    # pending → claimed → done | failed; or cancelled (once the extension had it).
    status: Mapped[str] = mapped_column(String(16))
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    timeout_seconds: Mapped[float] = mapped_column(Float)
    # The extension took this call and has yet to hear that it was stopped (cancelled by
    # the driver, or out of time).
    cancelled: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_browser_calls_link_status", "link_id", "status", "created_at"),
        Index("ix_browser_calls_user", "user_id"),
    )


class UsageEvent(Base):
    """One usage event from this site's pages, kept so they can be searched here as well as
    in Mixpanel (apps/cloud/usage_events.py, migration 0068, 2026-11-15). What the page did,
    never what the person wrote: the pages mask names, text and ids before sending
    (ui/app/src/telemetry.ts). ``user_id`` is the signed-in person's Auth0 ``sub`` from their
    token, else null; ``anonymous_id`` is the random id the landing page keeps in the browser
    before sign-in. Deleted with the account (apps/cloud/account_deletion.py) and after
    ``events_retention_days``."""

    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # When it happened, as the page says (kept within a week before and an hour after it
    # arrived), and when it arrived.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    event: Mapped[str] = mapped_column(String(100))
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    anonymous_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # One page load of the web app, or one visit to the landing page.
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The page's own id for the event, which Mixpanel also gets as ``$insert_id``.
    insert_id: Mapped[str] = mapped_column(String(64))
    # "web" (the web app at /app/) or "landing" (the site's front page).
    surface: Mapped[str] = mapped_column(String(16))
    # The route with every name and id masked (``/home/agent/:name``), or the landing page's path.
    page: Mapped[str | None] = mapped_column(String(200), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    language: Mapped[str | None] = mapped_column(String(35), nullable=True)
    # The event's own properties (a click's label, a count): at most 8 KB.
    properties: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    __table_args__ = (
        Index("ix_usage_events_occurred", "occurred_at"),
        Index("ix_usage_events_user", "user_id", "occurred_at"),
        Index("ix_usage_events_anonymous", "anonymous_id", "occurred_at"),
        Index("ix_usage_events_event", "event", "occurred_at"),
        Index("ix_usage_events_properties", "properties", postgresql_using="gin", postgresql_ops={"properties": "jsonb_path_ops"}),
    )
