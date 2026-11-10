"""Поддерживаемый остаток фонда (подушка безопасности)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "accounts", sa.Column("reserve_level", sa.Numeric(precision=14, scale=2), nullable=True)
    )
    op.create_check_constraint(
        "ck_accounts_reserve_pos", "accounts", "reserve_level IS NULL OR reserve_level > 0"
    )


def downgrade() -> None:
    op.drop_constraint("ck_accounts_reserve_pos", "accounts", type_="check")
    op.drop_column("accounts", "reserve_level")
