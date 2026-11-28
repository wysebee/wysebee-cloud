"""Menu orders: the order a person put a menu's items in, by dragging them

The person's call (2026-11-28): the menus beside the rail (Workspaces first; Tasks and Agents
later) keep the order the person drags their items into. One row per person, menu and scope —
the list the items are in, e.g. a workspace's folder — holding the items' keys in order
(apps/cloud/menu_orders.py). Items not in it follow, by name.

Revision ID: 0087
Revises: 0086
Create Date: 2026-11-28 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = '0087'
down_revision: str | None = '0086'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "menu_orders",
        sa.Column("user_id", sa.String(255), primary_key=True),
        sa.Column("menu", sa.String(40), primary_key=True),
        sa.Column("scope", sa.String(500), primary_key=True),
        sa.Column("keys", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("menu_orders")
