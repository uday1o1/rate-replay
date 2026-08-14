"""Create immutable scenario and scenario-result storage.

Revision ID: 20260814_0005
Revises: 20260814_0004
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0005"
down_revision: str | None = "20260814_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scenarios",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), nullable=False),
        sa.Column("profile_version_id", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("tariff_version_id", sa.String(length=64), nullable=False),
        sa.Column("operation_request_hash", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_scenario_state",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["profile_version_id"], ["profile_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_scenarios_owner_created", "scenarios", ["owner_user_id", "created_at"])
    op.create_table(
        "scenario_loads",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("scenario_id", sa.String(length=32), nullable=False),
        sa.Column("load_id", sa.String(length=36), nullable=False),
        sa.Column("physical_asset_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("execution_spec_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["scenario_id"], ["scenarios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scenario_id", "load_id", name="uq_scenario_load_id"),
        sa.UniqueConstraint("scenario_id", "physical_asset_key", name="uq_scenario_physical_asset"),
    )
    op.create_table(
        "scenario_reference_schedules",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("scenario_load_id", sa.String(length=32), nullable=False),
        sa.Column("occurrence_id", sa.String(length=36), nullable=False),
        sa.Column("required_energy_wh", sa.BigInteger(), nullable=False),
        sa.Column("earliest_start_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schedule_hash", sa.String(length=64), nullable=False),
        sa.Column("schedule_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["scenario_load_id"], ["scenario_loads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scenario_load_id", "occurrence_id", name="uq_scenario_load_occurrence"
        ),
    )
    op.create_table(
        "scenario_results",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), nullable=False),
        sa.Column("scenario_id", sa.String(length=32), nullable=False),
        sa.Column("profile_version_id", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("operation_request_hash", sa.String(length=64), nullable=False),
        sa.Column("semantic_hash", sa.String(length=64), nullable=False),
        sa.Column("result_hash", sa.String(length=64), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["profile_version_id"], ["profile_versions.id"]),
        sa.ForeignKeyConstraint(["scenario_id"], ["scenarios.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
        sa.UniqueConstraint("scenario_id"),
        sa.UniqueConstraint("owner_user_id", "semantic_hash", name="uq_owner_scenario_semantic"),
    )
    op.create_index(
        "ix_scenario_results_owner_created",
        "scenario_results",
        ["owner_user_id", "created_at"],
    )
    op.alter_column("calculation_manifests", "replay_id", nullable=True)
    op.add_column(
        "calculation_manifests",
        sa.Column("scenario_result_id", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_manifest_scenario_result",
        "calculation_manifests",
        "scenario_results",
        ["scenario_result_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_manifest_scenario_result", "calculation_manifests", ["scenario_result_id"]
    )
    op.create_check_constraint(
        "ck_manifest_exactly_one_result",
        "calculation_manifests",
        "(replay_id IS NOT NULL AND scenario_result_id IS NULL) OR "
        "(replay_id IS NULL AND scenario_result_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_manifest_exactly_one_result", "calculation_manifests", type_="check")
    op.drop_constraint("uq_manifest_scenario_result", "calculation_manifests", type_="unique")
    op.drop_constraint("fk_manifest_scenario_result", "calculation_manifests", type_="foreignkey")
    op.drop_column("calculation_manifests", "scenario_result_id")
    op.alter_column("calculation_manifests", "replay_id", nullable=False)
    op.drop_index("ix_scenario_results_owner_created", table_name="scenario_results")
    op.drop_table("scenario_results")
    op.drop_table("scenario_reference_schedules")
    op.drop_table("scenario_loads")
    op.drop_index("ix_scenarios_owner_created", table_name="scenarios")
    op.drop_table("scenarios")
