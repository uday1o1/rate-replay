"""Add owner-scoped immutable redacted report exports.

Revision ID: 20260814_0013
Revises: 20260814_0012
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0013"
down_revision: str | None = "20260814_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_exports",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), nullable=False),
        sa.Column("scenario_id", sa.String(length=32), nullable=False),
        sa.Column("scenario_result_id", sa.String(length=32), nullable=False),
        sa.Column("profile_version_id", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("semantic_hash", sa.String(length=64), nullable=False),
        sa.Column("report_hash", sa.String(length=64), nullable=False),
        sa.Column("redaction_policy_version", sa.String(length=64), nullable=False),
        sa.Column("report_template_version", sa.String(length=64), nullable=False),
        sa.Column("content_json", sa.Text(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["profile_version_id"], ["profile_versions.id"]),
        sa.ForeignKeyConstraint(["scenario_id"], ["scenarios.id"]),
        sa.ForeignKeyConstraint(["scenario_result_id"], ["scenario_results.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", name="uq_report_export_job"),
        sa.UniqueConstraint("object_key", name="uq_report_export_object"),
        sa.UniqueConstraint(
            "owner_user_id",
            "semantic_hash",
            name="uq_owner_report_semantic",
        ),
    )
    op.create_index(
        "ix_report_exports_owner_created",
        "report_exports",
        ["owner_user_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_report_exports_owner_created", table_name="report_exports")
    op.drop_table("report_exports")
