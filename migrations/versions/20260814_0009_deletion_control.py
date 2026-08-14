"""Add the sweep-exempt deletion control plane.

Revision ID: 20260814_0009
Revises: 20260814_0008
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0009"
down_revision: str | None = "20260814_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_user_lifecycle",
        "users",
        "lifecycle_state IN ('ACTIVE', 'DELETION_PENDING_LEDGER', 'DELETING', 'DELETED')",
    )
    op.add_column("users", sa.Column("deletion_scope_id", sa.String(length=32)))
    op.create_unique_constraint("uq_users_deletion_scope_id", "users", ["deletion_scope_id"])

    op.create_table(
        "deletion_intents",
        sa.Column("deletion_id", sa.String(length=32), nullable=False),
        sa.Column("owner_user_id", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_schema_version", sa.String(length=64), nullable=False),
        sa.Column("canonical_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("receipt_digest", sa.String(length=64), nullable=False),
        sa.Column("original_generation", sa.Integer(), nullable=False),
        sa.Column("proposed_generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("preparation_digest", sa.String(length=64)),
        sa.Column("preparation_receipt", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prepared_at", sa.DateTime(timezone=True)),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("invalidated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "state IN ('INTENT_CREATED', 'PREPARED', 'CONSUMED', 'INVALIDATED')",
            name="ck_deletion_intent_state",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("deletion_id"),
        sa.UniqueConstraint("owner_user_id", name="uq_deletion_intent_owner"),
        sa.UniqueConstraint(
            "owner_user_id",
            "idempotency_key",
            name="uq_deletion_intent_idempotency",
        ),
    )
    op.create_index(
        "ix_deletion_intent_expiry",
        "deletion_intents",
        ["state", "expires_at"],
    )

    op.create_table(
        "deletion_receipts",
        sa.Column("deletion_id", sa.String(length=32), nullable=False),
        sa.Column("receipt_verifier", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("artifact_counts_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("verifier_expires_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('INTENT_CREATED', 'PREPARED', 'DELETION_PENDING_LEDGER', "
            "'DELETING', 'DRAIN', 'SWEEP', 'VERIFY', 'COMPLETE', 'DELETED', 'ABORTED')",
            name="ck_deletion_receipt_status",
        ),
        sa.PrimaryKeyConstraint("deletion_id"),
    )
    op.create_index(
        "ix_deletion_receipt_expiry",
        "deletion_receipts",
        ["verifier_expires_at"],
    )

    op.create_table(
        "deletion_ledger_receipts",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("deletion_id", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("canonical_digest", sa.String(length=64), nullable=False),
        sa.Column("integrity_receipt", sa.String(length=64), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "phase IN ('PREPARED', 'REQUESTED', 'COMPLETED', 'ABORTED')",
            name="ck_deletion_ledger_receipt_phase",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deletion_id", "phase", name="uq_deletion_ledger_phase"),
    )
    op.create_index(
        "ix_deletion_ledger_unresolved",
        "deletion_ledger_receipts",
        ["phase", "acknowledged_at"],
    )

    op.create_table(
        "deletion_control_operations",
        sa.Column("deletion_id", sa.String(length=32), nullable=False),
        sa.Column("target_scope_id", sa.String(length=32), nullable=False),
        sa.Column("scope_token", sa.String(length=64), nullable=False),
        sa.Column("restore_key_version", sa.String(length=32), nullable=False),
        sa.Column("original_generation", sa.Integer(), nullable=False),
        sa.Column("deletion_generation", sa.Integer(), nullable=False),
        sa.Column("preparation_digest", sa.String(length=64), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("deletion_job_id", sa.String(length=32)),
        sa.Column("artifact_counts_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "phase IN ('FENCE', 'REQUESTED', 'DRAIN', 'SWEEP', 'VERIFY', 'COMPLETE')",
            name="ck_deletion_control_phase",
        ),
        sa.ForeignKeyConstraint(["deletion_job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("deletion_id"),
        sa.UniqueConstraint("deletion_job_id", name="uq_deletion_control_job"),
        sa.UniqueConstraint("scope_token", name="uq_deletion_control_scope_token"),
        sa.UniqueConstraint("target_scope_id", name="uq_deletion_control_target_scope"),
    )

    op.create_table(
        "deletion_audit_tombstones",
        sa.Column("deletion_id", sa.String(length=32), nullable=False),
        sa.Column("receipt_verifier", sa.String(length=255), nullable=False),
        sa.Column("verifier_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope_token", sa.String(length=64), nullable=False),
        sa.Column("restore_key_version", sa.String(length=32), nullable=False),
        sa.Column("deletion_generation", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("artifact_counts_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("status_code", sa.String(length=64), nullable=False),
        sa.CheckConstraint("status = 'DELETED'", name="ck_deletion_audit_status"),
        sa.PrimaryKeyConstraint("deletion_id"),
        sa.UniqueConstraint("scope_token", name="uq_deletion_audit_scope_token"),
    )


def downgrade() -> None:
    op.drop_table("deletion_audit_tombstones")
    op.drop_table("deletion_control_operations")
    op.drop_index("ix_deletion_ledger_unresolved", table_name="deletion_ledger_receipts")
    op.drop_table("deletion_ledger_receipts")
    op.drop_index("ix_deletion_receipt_expiry", table_name="deletion_receipts")
    op.drop_table("deletion_receipts")
    op.drop_index("ix_deletion_intent_expiry", table_name="deletion_intents")
    op.drop_table("deletion_intents")
    op.drop_constraint("uq_users_deletion_scope_id", "users", type_="unique")
    op.drop_column("users", "deletion_scope_id")
    op.drop_constraint("ck_user_lifecycle", "users", type_="check")
