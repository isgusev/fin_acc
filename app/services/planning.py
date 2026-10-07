"""Планирование доходов и расходов, расчёт плановой зарплаты и налогов."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import exists, extract, func, select
from sqlalchemy.orm import Session

from app import schemas
from app.errors import ConflictError, NotFoundError, ValidationAppError
from app.models import (
    IncomeKind,
    IncomeKindCode,
    Operation,
    OperationType,
    OperationTypeCode,
    Planning,
    PriorIncome,
    User,
)
from app.services import accounts as accounts_svc
from app.services import references
from app.services.calc import advance_amount, clamp_day, holiday_dates, money, progressive_tax

ZERO = Decimal("0.00")
AUTO_TAX_KINDS = (IncomeKindCode.SALARY.value, IncomeKindCode.BONUS.value)


def get_plan(db: Session, user: User, plan_id: uuid.UUID) -> Planning:
    p = db.get(Planning, plan_id)
    if p is None or p.owner_id != user.id:
        raise NotFoundError("Запись планирования не найдена")
    return p


def list_plans(
    db: Session,
    user: User,
    *,
    year: int | None = None,
    operation_type: str | None = None,
) -> Sequence[Planning]:
    q = select(Planning).where(Planning.owner_id == user.id)
    if year is not None:
        q = q.where(extract("year", Planning.planned_date) == year)
    if operation_type is not None:
        q = q.join(OperationType, OperationType.id == Planning.operation_type_id).where(
            OperationType.code == operation_type
        )
    return db.scalars(q.order_by(Planning.planned_date, Planning.created_at)).all()


def facts(
    db: Session, plan_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[Decimal, list[uuid.UUID]]]:
    """Сумма факт и id связанных операций для каждого плана."""
    if not plan_ids:
        return {}
    res: dict[uuid.UUID, tuple[Decimal, list[uuid.UUID]]] = {}
    rows = db.execute(
        select(Operation.plan_id, Operation.id, Operation.amount).where(
            Operation.plan_id.in_(list(plan_ids))
        )
    ).all()
    for plan_id, op_id, amount in rows:
        total, ids = res.get(plan_id, (ZERO, []))
        res[plan_id] = (total + amount, [*ids, op_id])
    return res


def to_out(p: Planning, fact: tuple[Decimal, list[uuid.UUID]] | None = None) -> schemas.PlanningOut:
    amount_fact, op_ids = fact or (ZERO, [])
    return schemas.PlanningOut(
        id=p.id,
        planned_date=p.planned_date,
        name=p.name,
        amount_planned=p.amount_planned,
        amount_net=p.amount_net,
        is_taxable=p.is_taxable,
        tax_rate=p.tax_rate,
        operation_type=schemas.OperationTypeOut.model_validate(p.operation_type),
        income_kind=schemas.IncomeKindOut.model_validate(p.income_kind) if p.income_kind else None,
        funding_account_id=p.funding_account_id,
        funding_account_name=p.funding_account.name if p.funding_account else None,
        is_auto=p.is_auto,
        amount_fact=amount_fact,
        operation_ids=op_ids,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


def plans_out(db: Session, plans: Sequence[Planning]) -> list[schemas.PlanningOut]:
    f = facts(db, [p.id for p in plans])
    return [to_out(p, f.get(p.id)) for p in plans]


# ---------------------------------------------------------------- налоги


def get_prior_income(db: Session, user: User, year: int) -> Decimal:
    """Доход с начала года до начала учёта (0, если не указан)."""
    amount = db.scalar(
        select(PriorIncome.amount).where(PriorIncome.owner_id == user.id, PriorIncome.year == year)
    )
    return amount if amount is not None else ZERO


def _upsert_prior_income(db: Session, user: User, year: int, amount: Decimal) -> None:
    row = db.scalar(
        select(PriorIncome).where(PriorIncome.owner_id == user.id, PriorIncome.year == year)
    )
    if row is None:
        db.add(PriorIncome(owner_id=user.id, year=year, amount=amount))
    else:
        row.amount = amount
    db.flush()


def set_prior_income(db: Session, user: User, year: int, amount: Decimal) -> None:
    """Сохраняет доход до начала учёта и пересчитывает налоги плана за этот год."""
    _upsert_prior_income(db, user, year, amount)
    recompute_year_taxes(db, user, year)
    db.commit()


def recompute_year_taxes(db: Session, user: User, year: int) -> None:
    """Пересчитывает ставку и «Сумму за вычетом налога» у зарплаты и премий за год.

    База для прогрессивной шкалы = доход с начала года до начала учёта
    + «Сумма план» всех предыдущих (по дате) записей с типом «Доход» и видом
    «Зарплата» или «Премия» за тот же календарный год.
    """
    scale = references.tax_scale(db)
    plans = db.scalars(
        select(Planning)
        .join(IncomeKind, IncomeKind.id == Planning.income_kind_id)
        .where(
            Planning.owner_id == user.id,
            IncomeKind.code.in_(AUTO_TAX_KINDS),
            extract("year", Planning.planned_date) == year,
        )
        .order_by(Planning.planned_date, Planning.created_at, Planning.id)
    ).all()
    base = get_prior_income(db, user, year)
    for p in plans:
        if p.is_taxable:
            r = progressive_tax(base, p.amount_planned, scale)
            p.tax_rate = r.max_rate
            p.amount_net = r.net
        else:
            p.tax_rate = None
            p.amount_net = p.amount_planned
        base += p.amount_planned
    db.flush()


@dataclass
class _Resolved:
    fields: dict[str, object]
    auto_tax: bool


def _resolve(db: Session, user: User, data: schemas.PlanningIn) -> _Resolved:
    ot = references.operation_type_by_code(db, data.operation_type)
    fields: dict[str, object] = {
        "operation_type_id": ot.id,
        "planned_date": data.planned_date,
        "name": data.name,
        "amount_planned": data.amount_planned,
    }
    if data.operation_type == OperationTypeCode.INCOME:
        if data.income_kind_id is None:
            raise ValidationAppError("Для дохода укажите вид дохода", "income_kind_id")
        if data.funding_account_id is not None:
            raise ValidationAppError(
                "Источник финансирования указывается только для расходов", "funding_account_id"
            )
        kind = references.get_income_kind(db, data.income_kind_id)
        if not kind.is_active:
            raise ValidationAppError("Этот вид дохода отключён", "income_kind_id")
        auto_tax = kind.code in AUTO_TAX_KINDS
        is_taxable = True if kind.code == IncomeKindCode.SALARY.value else data.is_taxable
        fields |= {"income_kind_id": kind.id, "funding_account_id": None, "is_taxable": is_taxable}
        if auto_tax:
            # Ставка и сумма за вычетом налога считаются автоматически по шкале
            fields |= {"tax_rate": None, "amount_net": None}
        elif is_taxable:
            if data.tax_rate is None:
                raise ValidationAppError("Укажите ставку налога", "tax_rate")
            net = money(data.amount_planned * (Decimal(100) - data.tax_rate) / Decimal(100))
            fields |= {"tax_rate": data.tax_rate, "amount_net": net}
        else:
            fields |= {"tax_rate": None, "amount_net": data.amount_planned}
        return _Resolved(fields, auto_tax)

    # Расход
    if data.income_kind_id is not None:
        raise ValidationAppError("Вид дохода указывается только для доходов", "income_kind_id")
    if data.is_taxable or data.tax_rate is not None:
        raise ValidationAppError("Налогообложение применимо только к доходам", "is_taxable")
    funding_id = None
    if data.funding_account_id is not None:
        acc = accounts_svc.get_account(db, user, data.funding_account_id, "funding_account_id")
        funding_id = acc.id
    fields |= {
        "income_kind_id": None,
        "funding_account_id": funding_id,
        "is_taxable": False,
        "tax_rate": None,
        "amount_net": None,
    }
    return _Resolved(fields, False)


def _is_auto_tax(p: Planning) -> bool:
    return p.income_kind is not None and p.income_kind.code in AUTO_TAX_KINDS


def _linked_ops(db: Session, plan_id: uuid.UUID) -> list[tuple[str, str]]:
    return [
        (code, name)
        for code, name in db.execute(
            select(OperationType.code, OperationType.name)
            .join(Operation, Operation.operation_type_id == OperationType.id)
            .where(Operation.plan_id == plan_id)
        ).all()
    ]


def create_plan(db: Session, user: User, data: schemas.PlanningIn) -> Planning:
    r = _resolve(db, user, data)
    p = Planning(owner_id=user.id, **r.fields)
    db.add(p)
    db.flush()
    if r.auto_tax:
        recompute_year_taxes(db, user, data.planned_date.year)
    db.commit()
    db.refresh(p)
    return p


def update_plan(db: Session, user: User, plan_id: uuid.UUID, data: schemas.PlanningIn) -> Planning:
    p = get_plan(db, user, plan_id)
    linked = _linked_ops(db, p.id)
    if linked and any(code != data.operation_type for code, _ in linked):
        raise ConflictError(
            "С планом связаны операции другого типа — сначала измените или отвяжите их",
            "operation_type",
        )
    was_auto, old_year = _is_auto_tax(p), p.planned_date.year
    r = _resolve(db, user, data)
    for k, v in r.fields.items():
        setattr(p, k, v)
    db.flush()
    db.refresh(p)
    years = {data.planned_date.year} if r.auto_tax else set()
    if was_auto:
        years.add(old_year)
    for y in sorted(years):
        recompute_year_taxes(db, user, y)
    db.commit()
    db.refresh(p)
    return p


def delete_plan(db: Session, user: User, plan_id: uuid.UUID) -> None:
    p = get_plan(db, user, plan_id)
    if db.scalar(select(exists().where(Operation.plan_id == p.id))):
        raise ConflictError("С планом связаны операции — сначала удалите или отвяжите их")
    was_auto, year = _is_auto_tax(p), p.planned_date.year
    db.delete(p)
    db.flush()
    if was_auto:
        recompute_year_taxes(db, user, year)
    db.commit()


# ---------------------------------------------------------------- расчёт зарплаты


@dataclass
class SalaryCalcResult:
    created: list[Planning]
    replaced: int
    skipped: int


def check_advance_days(
    advance_day: int, salary_day: int | None, advance_calc_day: int | None
) -> None:
    """Согласованность «Даты аванса», «Даты зарплаты» и «Расчёта аванса» в профиле."""
    if advance_calc_day is None:
        raise ValidationAppError(
            "При указанной «Дате аванса» заполните «Расчёт аванса»", "advance_calc_day"
        )
    if advance_calc_day > advance_day:
        raise ValidationAppError(
            "«Расчёт аванса» не может быть позже «Даты аванса»: аванс выплачивается "
            "за уже отработанные дни",
            "advance_calc_day",
        )
    if salary_day is not None and salary_day == advance_day:
        raise ValidationAppError(
            "«Дата аванса» и «Дата зарплаты» должны различаться", "advance_day"
        )


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def salary_schedule(
    salary: Decimal,
    start_date: date,
    salary_day: int,
    advance_day: int | None,
    advance_calc_day: int | None,
    holidays: set[date],
) -> list[tuple[date, Decimal]]:
    """Плановые выплаты зарплаты с start_date: список (дата выплаты, сумма до налога).

    Без аванса — одна выплата в месяц на «Дату зарплаты» до конца года start_date.

    С авансом каждый «рабочий» месяц W делится на две выплаты:
      аванс    = зарплата / рабочих дней W × рабочих дней W с 1-го по «Расчёт аванса»
                 — выплачивается в месяце W на «Дату аванса»;
      зарплата = зарплата / рабочих дней W × оставшихся рабочих дней W
                 — выплачивается на «Дату зарплаты»: в следующем месяце, если она раньше
                 «Даты аванса» (зарплата 10-го за прошлый месяц), иначе в том же месяце.
    Поэтому 10 января выплачивается остаток за декабрь прошлого года, а остаток за декабрь
    года расчёта — 10 января следующего года (входит в налоговую базу следующего года).
    Выплаты с датой раньше start_date не создаются.
    """
    year = start_date.year
    out: list[tuple[date, Decimal]] = []
    if advance_day is None or advance_calc_day is None:
        for month in range(start_date.month, 13):
            out.append((clamp_day(year, month, salary_day), salary))
    else:
        pay_next_month = salary_day < advance_day
        # рабочий месяц, остаток за который может выплачиваться в месяце start_date
        wy, wm = _prev_month(year, start_date.month) if pay_next_month else (year, start_date.month)
        while (wy, wm) <= (year, 12):
            adv = advance_amount(salary, wy, wm, advance_calc_day, holidays)
            out.append((clamp_day(wy, wm, advance_day), adv))
            py, pm = _next_month(wy, wm) if pay_next_month else (wy, wm)
            out.append((clamp_day(py, pm, salary_day), money(salary - adv)))
            wy, wm = _next_month(wy, wm)
    return sorted((d, a) for d, a in out if d >= start_date and a > 0)


def calculate_salary(
    db: Session,
    user: User,
    start_date: date,
    replace: bool,
    prior_income: Decimal | None = None,
) -> SalaryCalcResult:
    """«Рассчитать плановую зарплату с <дата>» — до конца календарного года.

    Суммы и даты выплат — см. salary_schedule. Праздники исключаются из рабочих дней.
    replace=False — даты, на которые уже есть плановая зарплата, пропускаются.
    replace=True — существующие записи зарплаты в периоде удаляются и создаются заново
    (кроме уже связанных с фактическими операциями: они сохраняются).
    prior_income — доход с начала года до начала учёта; если передан, сохраняется
    для года start_date и учитывается в базе прогрессивной шкалы.
    """
    if user.salary_day is None:
        raise ValidationAppError("Заполните «Дату зарплаты» в профиле", "salary_day")
    if not user.salary or user.salary <= 0:
        raise ValidationAppError("Заполните «Зарплату» в профиле", "salary")
    if user.advance_day is not None:
        check_advance_days(user.advance_day, user.salary_day, user.advance_calc_day)

    year = start_date.year
    if prior_income is not None:
        _upsert_prior_income(db, user, year, prior_income)
    holidays: set[date] = set()
    for y in (year - 1, year, year + 1):
        holidays |= holiday_dates(references.holiday_ranges_for_year(db, y))
    income = references.operation_type_by_code(db, OperationTypeCode.INCOME.value)
    salary_kind = references.income_kind_by_code(db, IncomeKindCode.SALARY.value)

    planned = salary_schedule(
        user.salary, start_date, user.salary_day, user.advance_day, user.advance_calc_day, holidays
    )
    end_date = max([date(year, 12, 31), *(d for d, _ in planned)])

    existing = db.scalars(
        select(Planning).where(
            Planning.owner_id == user.id,
            Planning.income_kind_id == salary_kind.id,
            Planning.planned_date >= start_date,
            Planning.planned_date <= end_date,
        )
    ).all()
    linked_ids = (
        set(
            db.scalars(
                select(Operation.plan_id).where(Operation.plan_id.in_([p.id for p in existing]))
            ).all()
        )
        if existing
        else set()
    )

    busy_dates: set[date] = set()
    replaced = 0
    for p in existing:
        if replace and p.id not in linked_ids:
            db.delete(p)
            replaced += 1
        else:
            busy_dates.add(p.planned_date)
    db.flush()

    created: list[Planning] = []
    skipped = 0
    for d, amount in planned:
        if d in busy_dates:
            skipped += 1
            continue
        p = Planning(
            owner_id=user.id,
            planned_date=d,
            name=None,
            amount_planned=amount,
            is_taxable=True,
            operation_type_id=income.id,
            income_kind_id=salary_kind.id,
            is_auto=True,
        )
        db.add(p)
        created.append(p)
    db.flush()
    for y in sorted({year, end_date.year}):
        recompute_year_taxes(db, user, y)
    db.commit()
    for p in created:
        db.refresh(p)
    return SalaryCalcResult(created=created, replaced=replaced, skipped=skipped)


def year_totals(db: Session, user: User, year: int) -> dict[str, Decimal]:
    """Итоги плана за год: доходы (план/после налога), расходы."""
    rows = db.execute(
        select(
            OperationType.code,
            func.coalesce(func.sum(Planning.amount_planned), 0),
            func.coalesce(func.sum(func.coalesce(Planning.amount_net, Planning.amount_planned)), 0),
        )
        .join(OperationType, OperationType.id == Planning.operation_type_id)
        .where(Planning.owner_id == user.id, extract("year", Planning.planned_date) == year)
        .group_by(OperationType.code)
    ).all()
    out = {"income": ZERO, "income_net": ZERO, "expense": ZERO}
    for code, total, net in rows:
        if code == OperationTypeCode.INCOME.value:
            out["income"], out["income_net"] = Decimal(total), Decimal(net)
        elif code == OperationTypeCode.EXPENSE.value:
            out["expense"] = Decimal(total)
    return out
