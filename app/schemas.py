"""Pydantic-схемы входных и выходных данных (общие для JSON API и HTML-форм)."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models import ReplenishPeriod, UserRole

# Положительная сумма в рублях с копейками
PositiveMoney = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)]
NonNegMoney = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]
Rate = Annotated[Decimal, Field(ge=0, le=100, max_digits=5, decimal_places=2)]
DayOfMonth = Annotated[int, Field(ge=1, le=31)]
Name100 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Name200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
OptText200 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None
OptComment = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None


def _empty_to_none(v: object) -> object:
    if isinstance(v, str) and not v.strip():
        return None
    return v


class InModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- auth / профиль


class RegisterIn(InModel):
    email: EmailStr
    password: Annotated[str, StringConstraints(max_length=128)]
    display_name: Name100


class LoginIn(InModel):
    email: Annotated[str, StringConstraints(max_length=254)]
    password: Annotated[str, StringConstraints(max_length=128)]


class PasswordChangeIn(InModel):
    current_password: Annotated[str, StringConstraints(max_length=128)]
    new_password: Annotated[str, StringConstraints(max_length=128)]


class ProfileIn(InModel):
    display_name: Name100
    salary: NonNegMoney | None = None
    advance_day: DayOfMonth | None = None
    salary_day: DayOfMonth | None = None
    advance_calc_day: DayOfMonth | None = None

    _norm = field_validator(
        "salary", "advance_day", "salary_day", "advance_calc_day", mode="before"
    )(_empty_to_none)


class UserOut(OutModel):
    id: uuid.UUID
    email: str
    display_name: str
    role: UserRole
    is_active: bool
    must_change_password: bool
    salary: Decimal | None
    advance_day: int | None
    salary_day: int | None
    advance_calc_day: int | None
    created_at: datetime


# ---------------------------------------------------------------- справочники


class AccountTypeOut(OutModel):
    id: int
    code: str | None
    name: str
    single_per_user: bool
    sort_order: int


class OperationTypeOut(OutModel):
    id: int
    code: str
    name: str


class IncomeKindOut(OutModel):
    id: int
    code: str | None
    name: str
    sort_order: int
    is_active: bool


class AccountTypeIn(InModel):
    name: Name100
    single_per_user: bool = False
    sort_order: int = Field(default=100, ge=0, le=10000)


class OperationTypeIn(InModel):
    name: Name100


class IncomeKindIn(InModel):
    name: Name100
    sort_order: int = Field(default=100, ge=0, le=10000)
    is_active: bool = True


class ExpenseCategoryOut(OutModel):
    id: int
    name: str
    sort_order: int
    is_active: bool


class ExpenseCategoryIn(InModel):
    name: Name100
    sort_order: int = Field(default=100, ge=0, le=10000)
    is_active: bool = True


class TaxBracketIn(InModel):
    income_from: NonNegMoney
    income_to: PositiveMoney | None = None
    rate: Rate

    _norm = field_validator("income_to", mode="before")(_empty_to_none)

    @model_validator(mode="after")
    def _check_range(self) -> "TaxBracketIn":
        if self.income_to is not None and self.income_to <= self.income_from:
            raise ValueError("«Сумма дохода до» должна быть больше «Суммы дохода от»")
        return self


class TaxBracketOut(OutModel):
    id: int
    income_from: Decimal
    income_to: Decimal | None
    rate: Decimal


class HolidayIn(InModel):
    date_from: date
    date_to: date | None = None  # по умолчанию = date_from
    name: Name200

    _norm = field_validator("date_to", mode="before")(_empty_to_none)

    @model_validator(mode="after")
    def _fill_and_check(self) -> "HolidayIn":
        if self.date_to is None:
            self.date_to = self.date_from
        if self.date_to < self.date_from:
            raise ValueError("«Дата до» не может быть раньше «Даты от»")
        return self


class HolidayOut(OutModel):
    id: int
    date_from: date
    date_to: date
    name: str


class AppSettingsIn(InModel):
    registration_open: bool


# ---------------------------------------------------------------- счета


class AccountIn(InModel):
    name: Name100
    account_type_id: int
    replenish_period: ReplenishPeriod = ReplenishPeriod.NONE
    replenish_amount: PositiveMoney | None = None
    months_to_goal: int | None = Field(default=None, ge=1, le=1200)  # None = бессрочно
    # Только для фондов: при заданной целевой сумме сумма пополнения рассчитывается
    target_amount: PositiveMoney | None = None

    _norm = field_validator("replenish_amount", "months_to_goal", "target_amount", mode="before")(
        _empty_to_none
    )


class AccountOut(OutModel):
    id: uuid.UUID
    name: str
    account_type: AccountTypeOut
    replenish_period: ReplenishPeriod
    replenish_amount: Decimal | None
    months_to_goal: int | None
    target_amount: Decimal | None
    is_closed: bool
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    balance: Decimal = Decimal("0.00")


# ---------------------------------------------------------------- операции

OperationTypeLiteral = Literal["income", "expense", "transfer"]


class OperationIn(InModel):
    operation_type: OperationTypeLiteral
    # Для дохода счёт подставляется автоматически («Нераспределённый доход»)
    account_id: uuid.UUID | None = None
    target_account_id: uuid.UUID | None = None
    op_date: date
    name: OptText200 = None
    amount: PositiveMoney
    category_id: int | None = None  # только для расходов; обязательна со счёта «Текущий»
    comment: OptComment = None
    plan_id: uuid.UUID | None = None

    _norm = field_validator(
        "account_id",
        "target_account_id",
        "name",
        "category_id",
        "comment",
        "plan_id",
        mode="before",
    )(_empty_to_none)


class ImportRowIn(InModel):
    """Строка проверенной выписки → расход."""

    op_date: date
    amount: PositiveMoney
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)
    ]
    card: Annotated[str, StringConstraints(pattern=r"^\d{4}$")] | None = None
    name: OptText200 = None
    account_id: uuid.UUID | None = None
    category_id: int | None = None
    plan_id: uuid.UUID | None = None
    comment: OptComment = None

    _norm = field_validator(
        "card", "name", "account_id", "category_id", "plan_id", "comment", mode="before"
    )(_empty_to_none)


class ImportTextIn(InModel):
    text: Annotated[str, StringConstraints(max_length=200_000)]  # ~1500 операций


class ImportSaveIn(InModel):
    rows: list[ImportRowIn] = Field(min_length=1, max_length=2000)


class OperationOut(OutModel):
    id: uuid.UUID
    operation_type: OperationTypeOut
    account_id: uuid.UUID
    account_name: str
    target_account_id: uuid.UUID | None
    target_account_name: str | None
    op_date: date
    name: str | None
    amount: Decimal
    category_id: int | None
    category_name: str | None
    comment: str | None
    bank_description: str | None = None
    plan_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class OperationFilter(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
    account_type_id: int | None = None
    account_id: uuid.UUID | None = None
    operation_type: OperationTypeLiteral | None = None
    limit: int = Field(default=30, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)

    _norm = field_validator(
        "date_from", "date_to", "account_type_id", "account_id", "operation_type", mode="before"
    )(_empty_to_none)


# ---------------------------------------------------------------- планирование


class PlanningIn(InModel):
    operation_type: Literal["income", "expense"]
    planned_date: date
    name: OptText200 = None
    amount_planned: PositiveMoney
    is_taxable: bool = False
    tax_rate: Rate | None = None
    income_kind_id: int | None = None
    funding_account_id: uuid.UUID | None = None

    _norm = field_validator(
        "name", "tax_rate", "income_kind_id", "funding_account_id", mode="before"
    )(_empty_to_none)


class PlanningOut(OutModel):
    id: uuid.UUID
    planned_date: date
    name: str | None
    amount_planned: Decimal
    amount_net: Decimal | None
    is_taxable: bool
    tax_rate: Decimal | None
    operation_type: OperationTypeOut
    income_kind: IncomeKindOut | None
    funding_account_id: uuid.UUID | None
    funding_account_name: str | None
    is_auto: bool
    amount_fact: Decimal = Decimal("0.00")  # Сумма факт = сумма связанных операций
    operation_ids: list[uuid.UUID] = []
    created_at: datetime
    updated_at: datetime


class SalaryCalcIn(InModel):
    start_date: date
    replace: bool = False
    # Доход с начала года до начала учёта; если передан — сохраняется для года start_date
    prior_income: NonNegMoney | None = None

    _norm = field_validator("prior_income", mode="before")(_empty_to_none)


class PriorIncomeIn(InModel):
    amount: NonNegMoney


class PriorIncomeOut(BaseModel):
    year: int
    amount: Decimal


class SalaryCalcOut(BaseModel):
    created: int
    replaced: int
    skipped: int
    plans: list[PlanningOut]


# ---------------------------------------------------------------- сводка


class TypeBalanceOut(BaseModel):
    account_type_id: int
    account_type_name: str
    balance: Decimal


class FundReopenOut(BaseModel):
    old_account_id: uuid.UUID
    new_account: AccountOut
    transferred: Decimal
