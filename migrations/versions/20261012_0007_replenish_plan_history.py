"""История плана пополнения счетов (норма в истории пополнений не пересчитывается задним числом)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_replenish_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column(
            "period",
            postgresql.ENUM(name="replenish_period", create_type=False),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("amount IS NULL OR amount > 0", name="ck_replenish_plan_amount_pos"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_replenish_plan_account_from",
        "account_replenish_plans",
        ["account_id", "effective_from"],
        unique=True,
    )
    # Текущий план каждого счёта считаем действующим «с самого начала»
    op.execute(
        sa.text(
            "INSERT INTO account_replenish_plans (account_id, effective_from, period, amount) "
            "SELECT id, DATE '2000-01-01', replenish_period, replenish_amount FROM accounts"
        )
    )


def downgrade() -> None:
    op.drop_index("uq_replenish_plan_account_from", table_name="account_replenish_plans")
    op.drop_table("account_replenish_plans")
