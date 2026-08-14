"""Add coordinator support and permit intents after invalidation.

Revision ID: 20260814_0010
Revises: 20260814_0009
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0010"
down_revision: str | None = "20260814_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deletion_control_operations",
        sa.Column("intent_proof_digest", sa.String(length=64), nullable=True),
    )
    op.execute(
        "UPDATE deletion_control_operations "
        "SET intent_proof_digest = repeat('0', 64) "
        "WHERE intent_proof_digest IS NULL"
    )
    op.alter_column(
        "deletion_control_operations",
        "intent_proof_digest",
        nullable=False,
    )
    op.drop_constraint("uq_deletion_intent_owner", "deletion_intents", type_="unique")
    op.create_index(
        "uq_deletion_intent_active_owner",
        "deletion_intents",
        ["owner_user_id"],
        unique=True,
        postgresql_where="state != 'INVALIDATED'",
    )


def downgrade() -> None:
    op.drop_index("uq_deletion_intent_active_owner", table_name="deletion_intents")
    op.create_unique_constraint(
        "uq_deletion_intent_owner",
        "deletion_intents",
        ["owner_user_id"],
    )
    op.drop_column("deletion_control_operations", "intent_proof_digest")
