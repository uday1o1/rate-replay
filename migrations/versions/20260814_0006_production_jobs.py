"""Generalize durable jobs across production scope modes.

Revision ID: 20260814_0006
Revises: 20260814_0005
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0006"
down_revision: str | None = "20260814_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("jobs", "owner_user_id", existing_type=sa.String(length=32), nullable=True)
    op.alter_column("jobs", "import_id", existing_type=sa.String(length=32), nullable=True)
    op.alter_column(
        "jobs",
        "captured_import_generation",
        existing_type=sa.Integer(),
        nullable=True,
    )
    op.add_column(
        "jobs",
        sa.Column("request_json", sa.Text(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "jobs",
        sa.Column("profile_version_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("captured_profile_generation", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_jobs_profile_version",
        "jobs",
        "profile_versions",
        ["profile_version_id"],
        ["id"],
    )
    op.execute(
        sa.text(
            "UPDATE jobs SET profile_version_id = replay_results.profile_version_id "
            "FROM replay_results WHERE replay_results.job_id = jobs.id AND jobs.kind = 'REPLAY'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE jobs SET profile_version_id = comparison_results.profile_version_id "
            "FROM comparison_results WHERE comparison_results.job_id = jobs.id "
            "AND jobs.kind = 'COMPARISON'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE jobs SET profile_version_id = scenarios.profile_version_id "
            "FROM scenarios WHERE scenarios.job_id = jobs.id AND jobs.kind = 'SCENARIO'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE jobs SET captured_profile_generation = profile_versions.lifecycle_generation "
            "FROM profile_versions WHERE profile_versions.id = jobs.profile_version_id"
        )
    )
    op.create_check_constraint(
        "ck_job_kind",
        "jobs",
        "kind IN ('IMPORT', 'REPLAY', 'COMPARISON', 'SCENARIO', 'REPORT', 'RETENTION', 'DELETION')",
    )
    op.create_check_constraint(
        "ck_job_scope_mode",
        "jobs",
        "scope_mode IN ('ACTIVE_SCOPE', 'DELETING_SCOPE', 'SYSTEM_SCOPE')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_job_scope_mode", "jobs", type_="check")
    op.drop_constraint("ck_job_kind", "jobs", type_="check")
    op.drop_constraint("fk_jobs_profile_version", "jobs", type_="foreignkey")
    op.drop_column("jobs", "captured_profile_generation")
    op.drop_column("jobs", "profile_version_id")
    op.drop_column("jobs", "request_json")
    op.alter_column(
        "jobs",
        "captured_import_generation",
        existing_type=sa.Integer(),
        nullable=False,
    )
    op.alter_column("jobs", "import_id", existing_type=sa.String(length=32), nullable=False)
    op.alter_column("jobs", "owner_user_id", existing_type=sa.String(length=32), nullable=False)
