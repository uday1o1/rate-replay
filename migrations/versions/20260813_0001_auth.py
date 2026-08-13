"""Create local authentication tables.

Revision ID: 20260813_0001
Revises:
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("username_canonical", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username_canonical"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_sessions_idle_expiry", "sessions", ["idle_expires_at"])
    op.create_index("ix_sessions_user_active", "sessions", ["user_id", "revoked_at"])


def downgrade() -> None:
    op.drop_index("ix_sessions_user_active", table_name="sessions")
    op.drop_index("ix_sessions_idle_expiry", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("users")
