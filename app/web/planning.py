"""«Планирование»: план доходов и расходов на год, расчёт плановой зарплаты."""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app import schemas
from app.db import get_db
from app.deps import csrf_protect, current_user
from app.errors import AppError, ConflictError
from app.models import OperationTypeCode, Planning, User
from app.services import accounts as accounts_svc
from app.services import planning as plan_svc
from app.services import references
from app.web.forms import (
    FormInvalid,
    FormState,
    checkbox,
    form_values,
    norm_money,
    opt,
    safe_next,
    validate,
)
from app.web.templating import fmt_money_input, fmt_rate, redirect, render

router = APIRouter()

MIN_YEAR, MAX_YEAR = 2000, 2100


def _year(raw: str | None) -> int:
    if raw and raw.strip().isdigit():
        y = int(raw.strip())
        if MIN_YEAR <= y <= MAX_YEAR:
            return y
    return date.today().year


def _render_planning(
    request: Request,
    db: Session,
    user: User,
    year: int,
    salary_form: FormState | None = None,
    status_code: int = 200,
) -> Response:
    plans = plan_svc.plans_out(db, plan_svc.list_plans(db, user, year=year))
    default_start = date.today() if date.today().year == year else date(year, 1, 1)
    salary_form = salary_form or FormState(values={"start_date": default_start.isoformat()})
    profile_incomplete = not user.salary or user.salary_day is None
    return render(
        request,
        "planning.html",
        {
            "year": year,
            "plans": plans,
            "totals": plan_svc.year_totals(db, user, year),
            "sf": salary_form,
            "profile_incomplete": profile_incomplete,
            "auto_tax_kinds": plan_svc.AUTO_TAX_KINDS,
        },
        status_code=status_code,
    )


@router.get("/planning")
def planning_page(
    request: Request,
    year: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    return _render_planning(request, db, user, _year(year))


@router.post("/planning/salary", dependencies=[Depends(csrf_protect)])
async def planning_salary(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    values = form_values(await request.form())
    f = FormState(values=values)
    year = _year(values.get("year"))
    try:
        data = validate(
            schemas.SalaryCalcIn,
            {"start_date": opt(values, "start_date"), "replace": checkbox(values, "replace")},
        )
        r = plan_svc.calculate_salary(db, user, data.start_date, data.replace)
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_planning(request, db, user, year, f, status_code=422)
    return redirect(
        f"/planning?year={data.start_date.year}",
        flash=f"Создано {len(r.created)}, заменено {r.replaced}, пропущено {r.skipped}",
    )


# ---------------------------------------------------------------- форма записи плана


def _plan_values(p: Planning) -> dict[str, str]:
    return {
        "operation_type": p.operation_type.code,
        "planned_date": p.planned_date.isoformat(),
        "name": p.name or "",
        "amount_planned": fmt_money_input(p.amount_planned),
        "is_taxable": "1" if p.is_taxable else "",
        "tax_rate": fmt_rate(p.tax_rate),
        "income_kind_id": str(p.income_kind_id) if p.income_kind_id else "",
        "funding_account_id": str(p.funding_account_id) if p.funding_account_id else "",
    }


def _plan_data(db: Session, values: dict[str, str]) -> schemas.PlanningIn:
    t = values.get("operation_type", "")
    is_income = t == OperationTypeCode.INCOME
    kind_raw = opt(values, "income_kind_id") if is_income else None
    is_taxable = is_income and checkbox(values, "is_taxable")
    tax_rate = norm_money(opt(values, "tax_rate")) if is_taxable else None
    if kind_raw and kind_raw.isdigit():
        kind = references.get_income_kind(db, int(kind_raw))
        if kind.code in plan_svc.AUTO_TAX_KINDS:
            tax_rate = None  # ставка рассчитывается автоматически по шкале
    return validate(
        schemas.PlanningIn,
        {
            "operation_type": t,
            "planned_date": opt(values, "planned_date"),
            "name": opt(values, "name"),
            "amount_planned": norm_money(opt(values, "amount_planned")),
            "is_taxable": is_taxable,
            "tax_rate": tax_rate,
            "income_kind_id": kind_raw,
            "funding_account_id": None if is_income else opt(values, "funding_account_id"),
        },
    )


def _render_plan_form(
    request: Request,
    db: Session,
    user: User,
    f: FormState,
    plan: Planning | None = None,
    status_code: int = 200,
) -> Response:
    kinds = list(references.income_kinds(db, only_active=True))
    if plan is not None and plan.income_kind is not None and plan.income_kind not in kinds:
        kinds.append(plan.income_kind)
    accounts = list(accounts_svc.list_accounts(db, user, include_closed=False))
    funding = plan.funding_account if plan is not None else None
    if funding is not None and funding not in accounts:
        accounts.append(funding)
    return render(
        request,
        "planning_form.html",
        {
            "f": f,
            "plan": plan,
            "income_kinds": kinds,
            "accounts": accounts,
            "auto_tax_kinds": plan_svc.AUTO_TAX_KINDS,
        },
        status_code=status_code,
    )


def _back_url(values: dict[str, str]) -> str:
    planned = values.get("planned_date", "")
    default = f"/planning?year={planned[:4]}" if planned[:4].isdigit() else "/planning"
    return safe_next(values.get("next"), default)


@router.get("/planning/new")
def plan_new(
    request: Request,
    type: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    values = {
        "operation_type": type if type in ("income", "expense") else "income",
        "planned_date": date.today().isoformat(),
    }
    return _render_plan_form(request, db, user, FormState(values=values, action="/planning/new"))


@router.post("/planning/new", dependencies=[Depends(csrf_protect)])
async def plan_create(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    values = form_values(await request.form())
    f = FormState(values=values, action="/planning/new")
    try:
        plan_svc.create_plan(db, user, _plan_data(db, values))
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_plan_form(request, db, user, f, status_code=422)
    return redirect(_back_url(values), flash="Запись планирования добавлена")


@router.get("/planning/{plan_id}/edit")
def plan_edit(
    request: Request,
    plan_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    p = plan_svc.get_plan(db, user, plan_id)
    f = FormState(values=_plan_values(p), action=f"/planning/{p.id}/edit", edit_id=p.id)
    return _render_plan_form(request, db, user, f, p)


@router.post("/planning/{plan_id}/edit", dependencies=[Depends(csrf_protect)])
async def plan_update(
    request: Request,
    plan_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    p = plan_svc.get_plan(db, user, plan_id)
    values = form_values(await request.form())
    f = FormState(values=values, action=f"/planning/{p.id}/edit", edit_id=p.id)
    try:
        plan_svc.update_plan(db, user, plan_id, _plan_data(db, values))
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_plan_form(request, db, user, f, p, status_code=422)
    return redirect(_back_url(values), flash="Запись планирования сохранена")


@router.post("/planning/{plan_id}/delete", dependencies=[Depends(csrf_protect)])
async def plan_delete(
    request: Request,
    plan_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    p = plan_svc.get_plan(db, user, plan_id)
    nxt = safe_next(
        form_values(await request.form()).get("next"), f"/planning?year={p.planned_date.year}"
    )
    try:
        plan_svc.delete_plan(db, user, plan_id)
    except ConflictError as exc:
        db.rollback()
        return redirect(nxt, flash=exc.message, error=True)
    return redirect(nxt, flash="Запись планирования удалена")
