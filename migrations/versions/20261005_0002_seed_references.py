"""Начальное заполнение справочников, шкалы НДФЛ и праздников

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05
"""
from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

account_types = sa.table(
    "account_types",
    sa.column("code", sa.String), sa.column("name", sa.String),
    sa.column("single_per_user", sa.Boolean), sa.column("sort_order", sa.Integer),
)
operation_types = sa.table(
    "operation_types",
    sa.column("code", sa.String), sa.column("name", sa.String), sa.column("sort_order", sa.Integer),
)
income_kinds = sa.table(
    "income_kinds",
    sa.column("code", sa.String), sa.column("name", sa.String),
    sa.column("sort_order", sa.Integer), sa.column("is_active", sa.Boolean),
)
tax_brackets = sa.table(
    "tax_brackets",
    sa.column("income_from", sa.Numeric), sa.column("income_to", sa.Numeric),
    sa.column("rate", sa.Numeric),
)
holidays = sa.table(
    "holidays",
    sa.column("date_from", sa.Date), sa.column("date_to", sa.Date), sa.column("name", sa.String),
)
app_settings = sa.table("app_settings", sa.column("key", sa.String), sa.column("value", sa.Text))


def upgrade() -> None:
    op.bulk_insert(account_types, [
        {"code": "current", "name": "Текущий", "single_per_user": True, "sort_order": 10},
        {"code": "monthly", "name": "Ежемесячные траты", "single_per_user": False, "sort_order": 20},
        {"code": "fund", "name": "Фонд", "single_per_user": False, "sort_order": 30},
        {"code": "savings", "name": "Накопления", "single_per_user": True, "sort_order": 40},
        {"code": "unallocated", "name": "Нераспределённый доход", "single_per_user": True,
         "sort_order": 50},
    ])
    op.bulk_insert(operation_types, [
        {"code": "income", "name": "Доход", "sort_order": 10},
        {"code": "expense", "name": "Расход", "sort_order": 20},
        {"code": "transfer", "name": "Перевод", "sort_order": 30},
    ])
    op.bulk_insert(income_kinds, [
        {"code": "salary", "name": "Зарплата", "sort_order": 10, "is_active": True},
        {"code": "bonus", "name": "Премия", "sort_order": 20, "is_active": True},
        {"code": None, "name": "Продажа", "sort_order": 30, "is_active": True},
        {"code": None, "name": "Аренда", "sort_order": 40, "is_active": True},
        {"code": None, "name": "Кэшбэк", "sort_order": 50, "is_active": True},
        {"code": None, "name": "Проценты", "sort_order": 60, "is_active": True},
        {"code": None, "name": "Иное", "sort_order": 70, "is_active": True},
    ])
    # Шкала НДФЛ для резидентов РФ с 2025 года (администратор может изменить)
    op.bulk_insert(tax_brackets, [
        {"income_from": 0, "income_to": 2_400_000, "rate": 13},
        {"income_from": 2_400_000, "income_to": 5_000_000, "rate": 15},
        {"income_from": 5_000_000, "income_to": 20_000_000, "rate": 18},
        {"income_from": 20_000_000, "income_to": 50_000_000, "rate": 20},
        {"income_from": 50_000_000, "income_to": None, "rate": 22},
    ])
    # Нерабочие праздничные дни 2026 года (проверьте по производственному календарю)
    op.bulk_insert(holidays, [
        {"date_from": date(2026, 1, 1), "date_to": date(2026, 1, 11),
         "name": "Новогодние каникулы и Рождество"},
        {"date_from": date(2026, 2, 23), "date_to": date(2026, 2, 23),
         "name": "День защитника Отечества"},
        {"date_from": date(2026, 3, 9), "date_to": date(2026, 3, 9),
         "name": "Международный женский день (перенос)"},
        {"date_from": date(2026, 5, 1), "date_to": date(2026, 5, 1),
         "name": "Праздник Весны и Труда"},
        {"date_from": date(2026, 5, 11), "date_to": date(2026, 5, 11),
         "name": "День Победы (перенос)"},
        {"date_from": date(2026, 6, 12), "date_to": date(2026, 6, 12), "name": "День России"},
        {"date_from": date(2026, 11, 4), "date_to": date(2026, 11, 4),
         "name": "День народного единства"},
        {"date_from": date(2026, 12, 31), "date_to": date(2026, 12, 31),
         "name": "Выходной (перенос с 3 января)"},
    ])
    op.bulk_insert(app_settings, [{"key": "registration_open", "value": "true"}])


def downgrade() -> None:
    for t in ("app_settings", "holidays", "tax_brackets", "income_kinds", "operation_types",
              "account_types"):
        op.execute(sa.text(f"DELETE FROM {t}"))  # noqa: S608
