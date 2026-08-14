"""Add durable deletion writer fences and expirable audit verifiers.

Revision ID: 20260814_0011
Revises: 20260814_0010
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0011"
down_revision: str | None = "20260814_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "deletion_audit_tombstones",
        "receipt_verifier",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.create_table(
        "deletion_fence_targets",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("deletion_id", sa.String(length=32), nullable=False),
        sa.Column("target_kind", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.String(length=32), nullable=False),
        sa.Column("observed_generation", sa.Integer(), nullable=False),
        sa.Column("observed_state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "target_kind IN ('JOB_ATTEMPT', 'UPLOAD')",
            name="ck_deletion_fence_target_kind",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "deletion_id",
            "target_kind",
            "target_id",
            name="uq_deletion_fence_target",
        ),
    )
    op.create_index(
        "ix_deletion_fence_unresolved",
        "deletion_fence_targets",
        ["deletion_id", "resolved_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_deletion_fence_unresolved", table_name="deletion_fence_targets")
    op.drop_table("deletion_fence_targets")
    op.execute(
        "UPDATE deletion_audit_tombstones SET receipt_verifier = '' WHERE receipt_verifier IS NULL"
    )
    op.alter_column(
        "deletion_audit_tombstones",
        "receipt_verifier",
        existing_type=sa.String(length=255),
        nullable=False,
    )
