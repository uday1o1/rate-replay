"""Create immutable comparison result storage.

Revision ID: 20260814_0004
Revises: 20260813_0003
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0004"
down_revision: str | None = "20260813_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "comparison_results",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), nullable=False),
        sa.Column("profile_version_id", sa.String(length=32), nullable=False),
        sa.Column("current_replay_id", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("operation_request_hash", sa.String(length=64), nullable=False),
        sa.Column("semantic_hash", sa.String(length=64), nullable=False),
        sa.Column("result_hash", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["current_replay_id"], ["replay_results.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["profile_version_id"], ["profile_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
        sa.UniqueConstraint("owner_user_id", "semantic_hash", name="uq_owner_comparison_semantic"),
    )
    op.create_index(
        "ix_comparisons_owner_created",
        "comparison_results",
        ["owner_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_comparisons_owner_created", table_name="comparison_results")
    op.drop_table("comparison_results")
