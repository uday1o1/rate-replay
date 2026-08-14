"""Add attempt-scoped artifact staging and semantic result claims.

Revision ID: 20260814_0007
Revises: 20260814_0006
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0007"
down_revision: str | None = "20260814_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("terminal_result_type", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("terminal_result_id", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("terminal_semantic_hash", sa.String(length=64), nullable=True),
    )
    op.create_table(
        "object_upload_registrations",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("fencing_generation", sa.Integer(), nullable=False),
        sa.Column("artifact_class", sa.String(length=32), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("upload_identifier", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "artifact_class IN ('REPORT', 'TRACE')",
            name="ck_upload_artifact_class",
        ),
        sa.CheckConstraint(
            "state IN ('REGISTERED', 'STAGED', 'ACCEPTED', 'DELETE_PENDING', 'DELETED')",
            name="ck_upload_state",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id",
            "fencing_generation",
            "artifact_class",
            name="uq_job_attempt_artifact_class",
        ),
        sa.UniqueConstraint("object_key", name="uq_upload_object_key"),
    )
    op.create_index(
        "ix_upload_cleanup",
        "object_upload_registrations",
        ["state", "updated_at"],
    )
    op.create_table(
        "job_result_claims",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), nullable=False),
        sa.Column("job_kind", sa.String(length=32), nullable=False),
        sa.Column("semantic_hash", sa.String(length=64), nullable=False),
        sa.Column("calculation_contract_version", sa.String(length=64), nullable=False),
        sa.Column("result_type", sa.String(length=32), nullable=False),
        sa.Column("result_id", sa.String(length=32), nullable=False),
        sa.Column("accepted_job_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["accepted_job_id"], ["jobs.id"]),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id",
            "job_kind",
            "semantic_hash",
            name="uq_owner_job_semantic_result",
        ),
        sa.UniqueConstraint("accepted_job_id", name="uq_result_claim_job"),
    )
    op.create_index(
        "ix_result_claim_owner_created",
        "job_result_claims",
        ["owner_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_result_claim_owner_created", table_name="job_result_claims")
    op.drop_table("job_result_claims")
    op.drop_index("ix_upload_cleanup", table_name="object_upload_registrations")
    op.drop_table("object_upload_registrations")
    op.drop_column("jobs", "terminal_semantic_hash")
    op.drop_column("jobs", "terminal_result_id")
    op.drop_column("jobs", "terminal_result_type")
