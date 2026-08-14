"""Persist restore scope identity before deletion intent creation.

Revision ID: 20260814_0012
Revises: 20260814_0011
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0012"
down_revision: str | None = "20260814_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        "UPDATE users SET deletion_scope_id = md5(id || ':restore-scope-v1') "
        "WHERE deletion_scope_id IS NULL"
    )
    op.alter_column(
        "users",
        "deletion_scope_id",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.add_column(
        "deletion_intents",
        sa.Column("target_scope_id", sa.String(length=32), nullable=True),
    )
    op.execute(
        "UPDATE deletion_intents AS intent "
        "SET target_scope_id = users.deletion_scope_id "
        "FROM users WHERE users.id = intent.owner_user_id"
    )
    op.alter_column(
        "deletion_intents",
        "target_scope_id",
        existing_type=sa.String(length=32),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("deletion_intents", "target_scope_id")
    op.alter_column(
        "users",
        "deletion_scope_id",
        existing_type=sa.String(length=32),
        nullable=True,
    )
