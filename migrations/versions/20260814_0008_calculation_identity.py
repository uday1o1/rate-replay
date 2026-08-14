"""Persist requested semantic identities on calculation jobs.

Revision ID: 20260814_0008
Revises: 20260814_0007
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0008"
down_revision: str | None = "20260814_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("requested_semantic_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("calculation_contract_version", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "calculation_contract_version")
    op.drop_column("jobs", "requested_semantic_hash")
