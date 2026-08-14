"""Create canonical import and durable job tables.

Revision ID: 20260813_0002
Revises: 20260813_0001
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0002"
down_revision: str | None = "20260813_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("lifecycle_state", sa.String(length=32), server_default="ACTIVE", nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("lifecycle_generation", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "imports",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_generation", sa.Integer(), nullable=False),
        sa.Column("adapter", sa.String(length=64), nullable=False),
        sa.Column("raw_content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("profile_version_id", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "lifecycle_state IN ('ACTIVE', 'DELETION_PENDING_LEDGER', 'DELETING', 'DELETED')",
            name="ck_import_lifecycle",
        ),
        sa.CheckConstraint(
            "state IN ('QUEUED', 'PROCESSING', 'READY', 'CONFIRMED', 'FAILED', 'DELETED')",
            name="ck_import_state",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_imports_owner_created", "imports", ["owner_user_id", "created_at"])
    op.create_table(
        "raw_objects",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), nullable=False),
        sa.Column("import_id", sa.String(length=32), nullable=False),
        sa.Column("object_key", sa.String(length=255), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["import_id"], ["imports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_id"),
        sa.UniqueConstraint("object_key", name="uq_raw_object_key"),
    )
    op.create_table(
        "operation_requests",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), nullable=False),
        sa.Column("route_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_schema_version", sa.String(length=64), nullable=False),
        sa.Column("canonical_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id", "route_id", "idempotency_key", name="uq_operation_identity"
        ),
    )
    op.create_table(
        "interval_readings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("import_id", sa.String(length=32), nullable=False),
        sa.Column("start_utc_ns", sa.BigInteger(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("energy_wh", sa.BigInteger(), nullable=False),
        sa.Column("flow_direction", sa.String(length=16), nullable=False),
        sa.Column("source_unit", sa.String(length=32), nullable=False),
        sa.Column("source_multiplier", sa.Integer(), nullable=False),
        sa.Column("source_reading_type", sa.String(length=64), nullable=False),
        sa.Column("source_service_category", sa.String(length=64), nullable=False),
        sa.Column("source_commodity", sa.String(length=64), nullable=False),
        sa.Column("source_accumulation_behavior", sa.String(length=64), nullable=False),
        sa.Column("source_data_qualifier", sa.String(length=64), nullable=False),
        sa.Column("source_time_attribute", sa.String(length=64), nullable=False),
        sa.Column("source_local_time_parameters_hash", sa.String(length=64), nullable=True),
        sa.Column("source_timezone_offset_seconds", sa.Integer(), nullable=True),
        sa.Column("source_dst_offset_seconds", sa.Integer(), nullable=True),
        sa.Column("quality_flags_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["import_id"], ["imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_id", "start_utc_ns", name="uq_import_reading_start"),
    )
    op.create_index(
        "ix_interval_readings_import_start",
        "interval_readings",
        ["import_id", "start_utc_ns"],
    )
    op.create_table(
        "import_quality_findings",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("import_id", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("field_path", sa.String(length=255), nullable=False),
        sa.Column("safe_value", sa.String(length=255), nullable=False),
        sa.Column("warning_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["import_id"], ["imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("import_id", "code", "field_path", name="uq_import_finding_identity"),
    )
    op.create_table(
        "profile_versions",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), nullable=False),
        sa.Column("import_id", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("canonical_content", sa.LargeBinary(), nullable=False),
        sa.Column("billing_period_start_utc_ns", sa.BigInteger(), nullable=False),
        sa.Column("billing_period_end_utc_ns", sa.BigInteger(), nullable=False),
        sa.Column("tariff_timezone", sa.String(length=64), nullable=False),
        sa.Column("interval_resolution_seconds", sa.Integer(), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=32), nullable=False),
        sa.Column("lifecycle_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["import_id"], ["imports.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_user_id", "content_hash", name="uq_owner_profile_content"),
    )
    op.create_index(
        "ix_profiles_owner_created", "profile_versions", ["owner_user_id", "created_at"]
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("request_schema_version", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("scope_mode", sa.String(length=32), nullable=False),
        sa.Column("import_id", sa.String(length=32), nullable=False),
        sa.Column("captured_account_generation", sa.Integer(), nullable=False),
        sa.Column("captured_import_generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("fencing_generation", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('QUEUED', 'LEASED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_job_state",
        ),
        sa.ForeignKeyConstraint(["import_id"], ["imports.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_lease_queue", "jobs", ["state", "not_before", "created_at"])
    op.create_table(
        "job_attempts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("fencing_generation", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_job_attempt_number"),
        sa.UniqueConstraint("job_id", "fencing_generation", name="uq_job_attempt_fence"),
    )


def downgrade() -> None:
    op.drop_table("job_attempts")
    op.drop_index("ix_jobs_lease_queue", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_profiles_owner_created", table_name="profile_versions")
    op.drop_table("profile_versions")
    op.drop_table("import_quality_findings")
    op.drop_index("ix_interval_readings_import_start", table_name="interval_readings")
    op.drop_table("interval_readings")
    op.drop_table("operation_requests")
    op.drop_table("raw_objects")
    op.drop_index("ix_imports_owner_created", table_name="imports")
    op.drop_table("imports")
    op.drop_column("users", "lifecycle_generation")
    op.drop_column("users", "lifecycle_state")
