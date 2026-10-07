"""Справочник категорий расходов, целевая сумма фонда

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08

Текстовое поле operations.category заменяется ссылкой на справочник expense_categories.
Уже введённые пользователями категории не теряются: значения, которых нет среди
стандартных, добавляются в справочник.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_CATEGORIES = ["Еда", "Транспорт", "Здоровье", "Маркетплейсы", "Хозяйство", "Разное"]


def upgrade() -> None:
    categories = op.create_table(
        "expense_categories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_expense_categories_name_lower",
        "expense_categories",
        [sa.literal_column("lower(name)")],
        unique=True,
    )
    op.bulk_insert(
        categories,
        [
            {"name": name, "sort_order": (i + 1) * 10, "is_active": True}
            for i, name in enumerate(DEFAULT_CATEGORIES)
        ],
    )
    # Категории, которые пользователи уже вводили текстом, — тоже в справочник
    op.execute(
        sa.text(
            "INSERT INTO expense_categories (name, sort_order, is_active) "
            "SELECT DISTINCT ON (lower(btrim(o.category))) btrim(o.category), 1000, true "
            "FROM operations o "
            "WHERE o.category IS NOT NULL AND btrim(o.category) <> '' "
            "AND NOT EXISTS (SELECT 1 FROM expense_categories c "
            "                WHERE lower(c.name) = lower(btrim(o.category)))"
        )
    )
    op.add_column("operations", sa.Column("category_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "operations_category_id_fkey",
        "operations",
        "expense_categories",
        ["category_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.execute(
        sa.text(
            "UPDATE operations o SET category_id = c.id FROM expense_categories c "
            "WHERE o.category IS NOT NULL AND lower(c.name) = lower(btrim(o.category))"
        )
    )
    op.drop_column("operations", "category")

    op.add_column(
        "accounts", sa.Column("target_amount", sa.Numeric(precision=14, scale=2), nullable=True)
    )
    op.create_check_constraint(
        "ck_accounts_target_pos", "accounts", "target_amount IS NULL OR target_amount > 0"
    )


def downgrade() -> None:
    op.drop_constraint("ck_accounts_target_pos", "accounts", type_="check")
    op.drop_column("accounts", "target_amount")

    op.add_column("operations", sa.Column("category", sa.String(length=100), nullable=True))
    op.execute(
        sa.text(
            "UPDATE operations o SET category = c.name FROM expense_categories c "
            "WHERE o.category_id = c.id"
        )
    )
    op.drop_constraint("operations_category_id_fkey", "operations", type_="foreignkey")
    op.drop_column("operations", "category_id")
    op.drop_table("expense_categories")
