"""Исходные данные банковской выписки в операции (импорт выписки Т-Банка)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("operations", sa.Column("bank_description", sa.String(length=300), nullable=True))
    op.add_column("operations", sa.Column("bank_card", sa.String(length=8), nullable=True))


def downgrade() -> None:
    op.drop_column("operations", "bank_card")
    op.drop_column("operations", "bank_description")
