"""ORM-модели.

Справочники (типы счетов, типы операций, виды дохода), шкала налогов и праздничные дни —
общие для всех пользователей и редактируются только администратором.
Счета, операции и планирование принадлежат конкретному пользователю (owner).
"""

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

MONEY = Numeric(14, 2)
RATE = Numeric(5, 2)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    """Системные дата создания и изменения записи (не путать с датой операции)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------- пользователи


class UserRole(enum.StrEnum):
    ADMIN = "admin"
    USER = "user"


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("salary IS NULL OR salary >= 0", name="ck_users_salary_nonneg"),
        CheckConstraint(
            "advance_day IS NULL OR advance_day BETWEEN 1 AND 31", name="ck_users_advance_day"
        ),
        CheckConstraint(
            "salary_day IS NULL OR salary_day BETWEEN 1 AND 31", name="ck_users_salary_day"
        ),
        CheckConstraint(
            "advance_calc_day IS NULL OR advance_calc_day BETWEEN 1 AND 31",
            name="ck_users_advance_calc_day",
        ),
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e]),
        default=UserRole.USER,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Поля профиля из ТЗ
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)  # Имя/Псевдоним
    salary: Mapped[Decimal | None] = mapped_column(MONEY)  # Зарплата (в месяц, до налога)
    advance_day: Mapped[int | None] = mapped_column(SmallInteger)  # Дата аванса (число месяца)
    salary_day: Mapped[int | None] = mapped_column(SmallInteger)  # Дата зарплаты (число месяца)
    advance_calc_day: Mapped[int | None] = mapped_column(SmallInteger)  # Расчёт аванса (1..31)

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN


class UserSession(Base):
    """Серверная сессия. В cookie хранится случайный токен, в БД — только его SHA-256."""

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Последняя активность (обновляется не чаще раза в 5 минут) — для выхода по бездействию
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(lazy="joined")


class AppSetting(Base):
    """Общие настройки приложения (ключ-значение), редактирует администратор."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


# ---------------------------------------------------------------- справочники


class AccountTypeCode(enum.StrEnum):
    CURRENT = "current"  # Текущий
    MONTHLY = "monthly"  # Ежемесячные траты
    FUND = "fund"  # Фонд
    SAVINGS = "savings"  # Накопления
    UNALLOCATED = "unallocated"  # Нераспределённый доход


class OperationTypeCode(enum.StrEnum):
    INCOME = "income"  # Приход / Доход
    EXPENSE = "expense"  # Расход
    TRANSFER = "transfer"  # Перевод между счетами (добавлено к ТЗ)


class IncomeKindCode(enum.StrEnum):
    SALARY = "salary"
    BONUS = "bonus"


class AccountType(Base):
    __tablename__ = "account_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # code заполнен у системных типов, на него опирается бизнес-логика
    code: Mapped[str | None] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    single_per_user: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)


class OperationType(Base):
    __tablename__ = "operation_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)


class IncomeKind(Base):
    __tablename__ = "income_kinds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str | None] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ExpenseCategory(Base):
    """Категория расхода (справочник, редактирует администратор)."""

    __tablename__ = "expense_categories"
    __table_args__ = (Index("uq_expense_categories_name_lower", text("lower(name)"), unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class TaxBracket(TimestampMixin, Base):
    """Шкала НДФЛ: годовой доход нарастающим итогом от/до → ставка."""

    __tablename__ = "tax_brackets"
    __table_args__ = (
        CheckConstraint("income_from >= 0", name="ck_tax_from_nonneg"),
        CheckConstraint("income_to IS NULL OR income_to > income_from", name="ck_tax_range"),
        CheckConstraint("rate >= 0 AND rate <= 100", name="ck_tax_rate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    income_from: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    income_to: Mapped[Decimal | None] = mapped_column(MONEY)  # NULL = без верхней границы
    rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)  # проценты


class Holiday(TimestampMixin, Base):
    __tablename__ = "holidays"
    __table_args__ = (CheckConstraint("date_to >= date_from", name="ck_holiday_range"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


# ---------------------------------------------------------------- данные пользователя


class ReplenishPeriod(enum.StrEnum):
    NONE = "none"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


REPLENISH_PERIOD_LABELS: dict[ReplenishPeriod, str] = {
    ReplenishPeriod.NONE: "Нет",
    ReplenishPeriod.WEEKLY: "Еженедельно",
    ReplenishPeriod.MONTHLY: "Ежемесячно",
    ReplenishPeriod.QUARTERLY: "Ежеквартально",
    ReplenishPeriod.YEARLY: "Ежегодно",
}


class Account(TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(
            "replenish_amount IS NULL OR replenish_amount > 0", name="ck_accounts_replenish_pos"
        ),
        CheckConstraint(
            "months_to_goal IS NULL OR months_to_goal > 0", name="ck_accounts_months_pos"
        ),
        CheckConstraint(
            "target_amount IS NULL OR target_amount > 0", name="ck_accounts_target_pos"
        ),
        # Имя открытого счёта уникально в пределах пользователя
        Index(
            "uq_accounts_owner_name_open",
            "owner_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("NOT is_closed"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    account_type_id: Mapped[int] = mapped_column(
        ForeignKey("account_types.id", ondelete="RESTRICT"), nullable=False
    )
    replenish_period: Mapped[ReplenishPeriod] = mapped_column(
        Enum(
            ReplenishPeriod, name="replenish_period", values_callable=lambda e: [m.value for m in e]
        ),
        default=ReplenishPeriod.NONE,
        nullable=False,
    )
    replenish_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    months_to_goal: Mapped[int | None] = mapped_column(Integer)  # NULL = бессрочно
    # Целевая сумма (только для фондов): по ней рассчитывается сумма регулярного пополнения
    target_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account_type: Mapped[AccountType] = relationship(lazy="joined")


class Planning(TimestampMixin, Base):
    __tablename__ = "planning"
    __table_args__ = (
        CheckConstraint("amount_planned > 0", name="ck_planning_amount_pos"),
        CheckConstraint("amount_net IS NULL OR amount_net >= 0", name="ck_planning_net_nonneg"),
        CheckConstraint(
            "tax_rate IS NULL OR (tax_rate >= 0 AND tax_rate <= 100)", name="ck_planning_tax_rate"
        ),
        Index("ix_planning_owner_date", "owner_id", "planned_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    planned_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    amount_planned: Mapped[Decimal] = mapped_column(MONEY, nullable=False)  # Сумма план
    amount_net: Mapped[Decimal | None] = mapped_column(MONEY)  # Сумма за вычетом налога
    is_taxable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tax_rate: Mapped[Decimal | None] = mapped_column(RATE)  # Ставка налога, %
    operation_type_id: Mapped[int] = mapped_column(
        ForeignKey("operation_types.id", ondelete="RESTRICT"), nullable=False
    )
    income_kind_id: Mapped[int | None] = mapped_column(
        ForeignKey("income_kinds.id", ondelete="RESTRICT")
    )
    funding_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT")
    )  # Источник финансирования (только для расходов)
    is_auto: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    operation_type: Mapped[OperationType] = relationship(lazy="joined")
    income_kind: Mapped[IncomeKind | None] = relationship(lazy="joined")
    funding_account: Mapped[Account | None] = relationship(lazy="joined")


class Operation(TimestampMixin, Base):
    """Операция со счётом.

    income / expense: account_id — счёт операции, target_account_id пуст.
    transfer: account_id — счёт-источник, target_account_id — счёт-назначение.
    """

    __tablename__ = "operations"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_operations_amount_pos"),
        CheckConstraint(
            "target_account_id IS NULL OR target_account_id <> account_id",
            name="ck_operations_transfer_distinct",
        ),
        Index("ix_operations_owner_date", "owner_id", "op_date"),
        Index("ix_operations_account", "account_id"),
        Index("ix_operations_target_account", "target_account_id"),
        Index("ix_operations_plan", "plan_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    operation_type_id: Mapped[int] = mapped_column(
        ForeignKey("operation_types.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    target_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    op_date: Mapped[date] = mapped_column(
        Date, nullable=False
    )  # Дата операции (вводит пользователь)
    name: Mapped[str | None] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("expense_categories.id", ondelete="RESTRICT")
    )  # Категория (только для расходов)
    comment: Mapped[str | None] = mapped_column(Text)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("planning.id", ondelete="RESTRICT")
    )
    # Исходные данные строки банковской выписки (для операций, внесённых импортом).
    # По ним при следующем импорте подсказываются наименование, счёт и категория.
    bank_description: Mapped[str | None] = mapped_column(String(300))
    bank_card: Mapped[str | None] = mapped_column(String(8))

    operation_type: Mapped[OperationType] = relationship(lazy="joined")
    account: Mapped[Account] = relationship(foreign_keys=[account_id], lazy="joined")
    target_account: Mapped[Account | None] = relationship(
        foreign_keys=[target_account_id], lazy="joined"
    )
    plan: Mapped[Planning | None] = relationship(lazy="joined")
    category: Mapped[ExpenseCategory | None] = relationship(lazy="joined")


class PriorIncome(TimestampMixin, Base):
    """Доход с начала года до начала учёта в приложении (зарплата и премии).

    На счета не зачисляется — учитывается только как начальная база прогрессивной
    шкалы НДФЛ за этот год.
    """

    __tablename__ = "prior_income"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_prior_income_nonneg"),
        CheckConstraint("year BETWEEN 2000 AND 2100", name="ck_prior_income_year"),
        UniqueConstraint("owner_id", "year", name="uq_prior_income_owner_year"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
