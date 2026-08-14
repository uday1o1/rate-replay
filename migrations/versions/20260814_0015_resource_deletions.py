"""Generalize durable deletion controls to import and profile targets.

Revision ID: 20260814_0015
Revises: 20260814_0014
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0015"
down_revision: str | None = "20260814_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("imports", sa.Column("deletion_scope_id", sa.String(length=32)))
    op.execute("UPDATE imports SET deletion_scope_id = id")
    op.alter_column("imports", "deletion_scope_id", nullable=False)
    op.create_unique_constraint(
        "uq_imports_deletion_scope_id",
        "imports",
        ["deletion_scope_id"],
    )

    op.add_column("profile_versions", sa.Column("deletion_scope_id", sa.String(length=32)))
    op.execute("UPDATE profile_versions SET deletion_scope_id = id")
    op.alter_column("profile_versions", "deletion_scope_id", nullable=False)
    op.create_unique_constraint(
        "uq_profile_versions_deletion_scope_id",
        "profile_versions",
        ["deletion_scope_id"],
    )
    op.create_check_constraint(
        "ck_profile_lifecycle",
        "profile_versions",
        "lifecycle_state IN ('ACTIVE', 'DELETION_PENDING_LEDGER', 'DELETING', 'DELETED')",
    )

    op.add_column(
        "deletion_intents",
        sa.Column("target_kind", sa.String(length=16), server_default="ACCOUNT", nullable=False),
    )
    op.create_check_constraint(
        "ck_deletion_intent_target_kind",
        "deletion_intents",
        "target_kind IN ('ACCOUNT', 'IMPORT', 'PROFILE')",
    )
    op.drop_index("uq_deletion_intent_active_owner", table_name="deletion_intents")
    op.create_index(
        "uq_deletion_intent_active_target",
        "deletion_intents",
        ["target_scope_id"],
        unique=True,
        postgresql_where=sa.text("state != 'INVALIDATED'"),
        sqlite_where=sa.text("state != 'INVALIDATED'"),
    )

    op.add_column(
        "deletion_control_operations",
        sa.Column("target_kind", sa.String(length=16), server_default="ACCOUNT", nullable=False),
    )
    op.create_check_constraint(
        "ck_deletion_control_target_kind",
        "deletion_control_operations",
        "target_kind IN ('ACCOUNT', 'IMPORT', 'PROFILE')",
    )

    op.add_column(
        "deletion_audit_tombstones",
        sa.Column("target_kind", sa.String(length=16), server_default="ACCOUNT", nullable=False),
    )
    op.create_check_constraint(
        "ck_deletion_audit_target_kind",
        "deletion_audit_tombstones",
        "target_kind IN ('ACCOUNT', 'IMPORT', 'PROFILE')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_deletion_audit_target_kind",
        "deletion_audit_tombstones",
        type_="check",
    )
    op.drop_column("deletion_audit_tombstones", "target_kind")
    op.drop_constraint(
        "ck_deletion_control_target_kind",
        "deletion_control_operations",
        type_="check",
    )
    op.drop_column("deletion_control_operations", "target_kind")
    op.drop_index("uq_deletion_intent_active_target", table_name="deletion_intents")
    op.create_index(
        "uq_deletion_intent_active_owner",
        "deletion_intents",
        ["owner_user_id"],
        unique=True,
        postgresql_where=sa.text("state != 'INVALIDATED'"),
        sqlite_where=sa.text("state != 'INVALIDATED'"),
    )
    op.drop_constraint("ck_deletion_intent_target_kind", "deletion_intents", type_="check")
    op.drop_column("deletion_intents", "target_kind")
    op.drop_constraint("ck_profile_lifecycle", "profile_versions", type_="check")
    op.drop_constraint(
        "uq_profile_versions_deletion_scope_id",
        "profile_versions",
        type_="unique",
    )
    op.drop_column("profile_versions", "deletion_scope_id")
    op.drop_constraint("uq_imports_deletion_scope_id", "imports", type_="unique")
    op.drop_column("imports", "deletion_scope_id")
