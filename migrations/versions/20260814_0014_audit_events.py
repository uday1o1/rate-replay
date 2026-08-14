"""Add immutable owner-scoped audit events.

Revision ID: 20260814_0014
Revises: 20260814_0013
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0014"
down_revision: str | None = "20260814_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), nullable=True),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint("sequence >= 0", name="ck_audit_event_sequence"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_hash"),
        sa.UniqueConstraint(
            "owner_user_id",
            "event_type",
            "subject_type",
            "subject_id",
            "sequence",
            name="uq_audit_event_transition",
        ),
    )
    op.create_index(
        "ix_audit_events_owner_recorded",
        "audit_events",
        ["owner_user_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_owner_recorded", table_name="audit_events")
    op.drop_table("audit_events")
