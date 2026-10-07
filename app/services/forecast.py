"""Прогноз накоплений по месяцам.

Старт — текущий остаток на открытых счетах «Накопления». Каждый месяц:

    + плановые доходы (за вычетом налога), по которым ещё нет факта
    − отчисления в фонды и на ежемесячные траты по их планам пополнения
    − плановые расходы из накоплений (источник — «Накопления» или не указан), без факта
    = изменение накоплений; остаток — нарастающим итогом.

Текущий месяц — только то, что ещё не случилось: доходы и расходы без факта (включая
просроченные), отчисления — остаток плана месяца за вычетом уже сделанных пополнений.
Уже внесённые операции отражены в балансах и повторно не учитываются.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models import (
    Account,
    AccountType,
    AccountTypeCode,
    Operation,
    OperationType,
    OperationTypeCode,
    Planning,
    User,
)
from app.services import accounts as accounts_svc
from app.services.balances import ZERO, account_balances
from app.services.calc import money

HORIZON_MONTHS = 12
ALLOCATION_TYPES = (AccountTypeCode.FUND.value, AccountTypeCode.MONTHLY.value)


@dataclass(frozen=True)
class ForecastRow:
    month: date  # первое число месяца
    label: str
    income: Decimal
    to_funds: Decimal
    to_monthly: Decimal
    expenses: Decimal
    change: Decimal
    balance: Decimal  # накопления на конец месяца
    is_current: bool
    after_last_income: bool  # плановых доходов на этот месяц и дальше уже нет


@dataclass(frozen=True)
class Forecast:
    start_balance: Decimal
    rows: list[ForecastRow]
    last_income_month: date | None


def _month_end(m: date) -> date:
    return accounts_svc.month_shift(m, 1) - timedelta(days=1)


def _plans_without_fact(db: Session, user: User, code: str, until: date) -> Sequence[Planning]:
    return db.scalars(
        select(Planning)
        .join(OperationType, OperationType.id == Planning.operation_type_id)
        .where(
            Planning.owner_id == user.id,
            OperationType.code == code,
            Planning.planned_date <= until,
            ~exists().where(Operation.plan_id == Planning.id),
        )
    ).all()


def _is_savings_expense(plan: Planning, savings_ids: set[uuid.UUID]) -> bool:
    return plan.funding_account_id is None or plan.funding_account_id in savings_ids


def build(
    db: Session, user: User, today: date | None = None, months: int = HORIZON_MONTHS
) -> Forecast:
    today = today or date.today()
    first = date(today.year, today.month, 1)
    last_end = _month_end(accounts_svc.month_shift(first, months - 1))

    open_accounts = db.scalars(
        select(Account).join(AccountType).where(Account.owner_id == user.id, ~Account.is_closed)
    ).all()
    savings = [a for a in open_accounts if a.account_type.code == AccountTypeCode.SAVINGS.value]
    savings_ids = {a.id for a in savings}
    allocation = [a for a in open_accounts if a.account_type.code in ALLOCATION_TYPES]

    balances = account_balances(db, user.id, [a.id for a in savings])
    start_balance = sum((balances.get(a.id, ZERO) for a in savings), ZERO)

    incomes = _plans_without_fact(db, user, OperationTypeCode.INCOME.value, last_end)
    expenses = [
        p
        for p in _plans_without_fact(db, user, OperationTypeCode.EXPENSE.value, last_end)
        if _is_savings_expense(p, savings_ids)
    ]
    plans_by_account = {a.id: accounts_svc.replenish_plans(db, a.id) for a in allocation}
    # уже сделанные пополнения текущего месяца (вычитаются из плана текущего месяца)
    first_end = _month_end(first)
    done_this_month = {
        a.id: sum(
            (
                o.amount
                for o in accounts_svc.replenishment_history(db, user, a.id)
                if first <= o.op_date <= first_end and not accounts_svc.is_reopen_transfer(o, a)
            ),
            ZERO,
        )
        for a in allocation
    }
    last_income = max((p.planned_date for p in incomes), default=None)
    last_income_month = date(last_income.year, last_income.month, 1) if last_income else None

    rows: list[ForecastRow] = []
    exact_balance = start_balance
    for i in range(months):
        m = accounts_svc.month_shift(first, i)
        end = _month_end(m)
        current = i == 0

        def in_month(d: date, m: date = m, end: date = end, cur: bool = current) -> bool:
            # в текущий месяц попадают и просроченные (дата прошла, факта нет)
            return d <= end if cur else m <= d <= end

        income = sum(
            (
                p.amount_net if p.amount_net is not None else p.amount_planned
                for p in incomes
                if in_month(p.planned_date)
            ),
            ZERO,
        )
        spent = sum((p.amount_planned for p in expenses if in_month(p.planned_date)), ZERO)

        alloc = dict.fromkeys(ALLOCATION_TYPES, ZERO)
        for a in allocation:
            plans = [p for p in plans_by_account[a.id] if p.effective_from <= end]
            if plans:
                norm = accounts_svc.period_norm(
                    plans[-1].period, plans[-1].amount, m, end, rounded=False
                )
            else:
                norm = accounts_svc.period_norm(
                    a.replenish_period, a.replenish_amount, m, end, rounded=False
                )
            if not norm:
                continue
            if current:
                norm = max(norm - done_this_month[a.id], ZERO)
            alloc[a.account_type.code or ""] += norm  # code задан: отобраны по ALLOCATION_TYPES

        change = income - alloc[AccountTypeCode.FUND.value] - alloc[AccountTypeCode.MONTHLY.value]
        change -= spent
        exact_balance += change  # копим без округления, округляем только для показа
        rows.append(
            ForecastRow(
                month=m,
                label=f"{accounts_svc.MONTHS_RU[m.month - 1]} {m.year}",
                income=income,
                to_funds=money(alloc[AccountTypeCode.FUND.value]),
                to_monthly=money(alloc[AccountTypeCode.MONTHLY.value]),
                expenses=spent,
                change=money(change),
                balance=money(exact_balance),
                is_current=current,
                after_last_income=last_income_month is None or m > last_income_month,
            )
        )
    return Forecast(start_balance=start_balance, rows=rows, last_income_month=last_income_month)
