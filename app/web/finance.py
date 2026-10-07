"""Главный экран, счета, операции, «Ежемесячные траты» и «Фонды»."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app import schemas
from app.db import get_db
from app.deps import csrf_protect, current_user
from app.errors import AppError, ConflictError, NotFoundError
from app.models import AccountTypeCode, Operation, OperationTypeCode, User
from app.services import accounts as accounts_svc
from app.services import balances, references
from app.services import operations as ops_svc
from app.services import planning as plan_svc
from app.web.forms import FormInvalid, FormState, form_values, norm_money, opt, safe_next, validate
from app.web.templating import fmt_money, fmt_money_input, redirect, render

router = APIRouter()

PAGE_SIZE = 30
FILTER_FIELDS = ("date_from", "date_to", "account_type_id", "account_id", "operation_type")


async def _form(request: Request) -> dict[str, str]:
    return form_values(await request.form())


# ---------------------------------------------------------------- главный экран


@router.get("/")
def dashboard(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    q = {k: request.query_params.get(k, "").strip() for k in FILTER_FIELDS}
    offset_raw = request.query_params.get("offset", "0")
    filter_error = None
    try:
        flt = schemas.OperationFilter.model_validate(
            {**q, "limit": PAGE_SIZE, "offset": offset_raw or "0"}
        )
    except ValidationError:
        filter_error = "Некорректные параметры фильтра — показаны последние операции"
        flt = schemas.OperationFilter(limit=PAGE_SIZE)
        q = dict.fromkeys(FILTER_FIELDS, "")
    ops, total = ops_svc.list_operations(db, user, flt)

    def page_url(offset: int) -> str:
        params = {k: v for k, v in q.items() if v}
        if offset:
            params["offset"] = str(offset)
        return "/?" + urlencode(params) if params else "/"

    prev_url = page_url(max(flt.offset - PAGE_SIZE, 0)) if flt.offset > 0 else None
    next_url = page_url(flt.offset + PAGE_SIZE) if flt.offset + PAGE_SIZE < total else None

    all_accounts = accounts_svc.list_accounts(db, user)
    return render(
        request,
        "dashboard.html",
        {
            "type_balances": balances.balances_by_type(db, user.id),
            "ops": ops,
            "total": total,
            "offset": flt.offset,
            "prev_url": prev_url,
            "next_url": next_url,
            "q": q,
            "filtered": any(q.values()),
            "filter_error": filter_error,
            "accounts": accounts_svc.accounts_with_balances(db, user, all_accounts),
            "account_types": references.account_types(db),
            "operation_types": references.operation_types(db),
        },
    )


# ---------------------------------------------------------------- счета


def _account_values(acc: Any) -> dict[str, str]:
    return {
        "name": acc.name,
        "account_type_id": str(acc.account_type_id),
        "replenish_period": acc.replenish_period.value,
        "replenish_amount": fmt_money_input(acc.replenish_amount),
        "months_to_goal": "" if acc.months_to_goal is None else str(acc.months_to_goal),
        "target_amount": fmt_money_input(acc.target_amount),
    }


def _account_data(values: dict[str, str]) -> schemas.AccountIn:
    return validate(
        schemas.AccountIn,
        {
            "name": values.get("name", ""),
            "account_type_id": opt(values, "account_type_id"),
            "replenish_period": opt(values, "replenish_period"),
            "replenish_amount": norm_money(opt(values, "replenish_amount")),
            "months_to_goal": opt(values, "months_to_goal"),
            "target_amount": norm_money(opt(values, "target_amount")),
        },
    )


def _render_account_form(
    request: Request, db: Session, f: FormState, account: Any = None, status_code: int = 200
) -> Response:
    return render(
        request,
        "account_form.html",
        {
            "f": f,
            "account": account,
            "account_types": references.account_types(db),
            "fund_type_code": AccountTypeCode.FUND.value,
            "period_months": {p.value: float(m) for p, m in accounts_svc.PERIOD_MONTHS.items()},
        },
        status_code=status_code,
    )


@router.get("/accounts/new")
def account_new(
    request: Request,
    type: str | None = None,
    next: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    values = {"replenish_period": "none", "next": safe_next(next)}
    if type:
        if type.isdigit():
            values["account_type_id"] = type
        else:
            for at in references.account_types(db):
                if at.code == type:
                    values["account_type_id"] = str(at.id)
    return _render_account_form(request, db, FormState(values=values, action="/accounts/new"))


@router.post("/accounts/new", dependencies=[Depends(csrf_protect)])
async def account_create(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    values = await _form(request)
    f = FormState(values=values, action="/accounts/new")
    try:
        acc = accounts_svc.create_account(db, user, _account_data(values))
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_account_form(request, db, f, status_code=422)
    return redirect(safe_next(values.get("next")), flash=f"Счёт «{acc.name}» создан")


@router.get("/accounts/{account_id}/edit")
def account_edit(
    request: Request,
    account_id: uuid.UUID,
    next: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    acc = accounts_svc.get_account(db, user, account_id)
    values = _account_values(acc) | {"next": safe_next(next)}
    f = FormState(values=values, action=f"/accounts/{acc.id}/edit", edit_id=acc.id)
    return _render_account_form(request, db, f, acc)


@router.post("/accounts/{account_id}/edit", dependencies=[Depends(csrf_protect)])
async def account_update(
    request: Request,
    account_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    acc = accounts_svc.get_account(db, user, account_id)
    values = await _form(request)
    f = FormState(values=values, action=f"/accounts/{acc.id}/edit", edit_id=acc.id)
    try:
        acc = accounts_svc.update_account(db, user, account_id, _account_data(values))
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_account_form(request, db, f, acc, status_code=422)
    return redirect(safe_next(values.get("next")), flash=f"Счёт «{acc.name}» сохранён")


@router.post("/accounts/{account_id}/delete", dependencies=[Depends(csrf_protect)])
async def account_delete(
    request: Request,
    account_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    nxt = safe_next((await _form(request)).get("next"))
    acc = accounts_svc.get_account(db, user, account_id)
    name = acc.name
    try:
        accounts_svc.delete_account(db, user, account_id)
    except ConflictError as exc:
        db.rollback()
        return redirect(nxt, flash=exc.message, error=True)
    return redirect(nxt, flash=f"Счёт «{name}» удалён")


@router.post("/accounts/{account_id}/close", dependencies=[Depends(csrf_protect)])
async def account_close(
    request: Request,
    account_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    nxt = safe_next((await _form(request)).get("next"))
    try:
        acc = accounts_svc.close_account(db, user, account_id)
    except ConflictError as exc:
        db.rollback()
        return redirect(nxt, flash=exc.message, error=True)
    return redirect(nxt, flash=f"Счёт «{acc.name}» закрыт")


@router.post("/accounts/{account_id}/reopen", dependencies=[Depends(csrf_protect)])
async def account_reopen(
    request: Request,
    account_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    nxt = safe_next((await _form(request)).get("next"), "/funds")
    try:
        _old, new, moved = accounts_svc.reopen_fund(db, user, account_id)
    except AppError as exc:
        if isinstance(exc, NotFoundError):
            raise
        db.rollback()
        return redirect(nxt, flash=exc.message, error=True)
    msg = f"Фонд «{new.name}» открыт заново"
    if moved != 0:
        msg += f", перенесён остаток {fmt_money(moved)} ₽"
    return redirect(nxt, flash=msg)


# ---------------------------------------------------------------- «Ежемесячные траты» и «Фонды»


def _account_group(
    request: Request, db: Session, user: User, code: AccountTypeCode, template_ctx: dict[str, Any]
) -> Response:
    accs = accounts_svc.list_accounts(db, user, type_code=code.value)
    items = accounts_svc.accounts_with_balances(db, user, accs)
    open_items = [a for a in items if not a.is_closed]
    closed_items = [a for a in items if a.is_closed]
    by_id = {a.id: a for a in accs}
    history = {a.id: accounts_svc.replenishment_summary(db, user, by_id[a.id]) for a in open_items}
    account_type = references.account_type_by_code(db, code.value)
    return render(
        request,
        "account_group.html",
        {
            **template_ctx,
            "account_type": account_type,
            "open_items": open_items,
            "closed_items": closed_items,
            "history": history,
            "total": sum((a.balance for a in open_items), Decimal("0.00")),
        },
    )


@router.get("/monthly")
def monthly(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    return _account_group(
        request,
        db,
        user,
        AccountTypeCode.MONTHLY,
        {"title": "Ежемесячные траты", "is_fund": False, "page_path": "/monthly"},
    )


@router.get("/funds")
def funds(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    return _account_group(
        request,
        db,
        user,
        AccountTypeCode.FUND,
        {"title": "Фонды", "is_fund": True, "page_path": "/funds"},
    )


# ---------------------------------------------------------------- операции


def _operation_values(op: Operation) -> dict[str, str]:
    return {
        "operation_type": op.operation_type.code,
        "account_id": str(op.account_id),
        "target_account_id": str(op.target_account_id) if op.target_account_id else "",
        "op_date": op.op_date.isoformat(),
        "name": op.name or "",
        "amount": fmt_money_input(op.amount),
        "category_id": str(op.category_id) if op.category_id else "",
        "comment": op.comment or "",
        "plan_id": str(op.plan_id) if op.plan_id else "",
    }


def _operation_data(values: dict[str, str]) -> schemas.OperationIn:
    t = values.get("operation_type", "")
    return validate(
        schemas.OperationIn,
        {
            "operation_type": t,
            # для дохода счёт подставляет сервис («Нераспределённый доход»)
            "account_id": None if t == OperationTypeCode.INCOME else opt(values, "account_id"),
            "target_account_id": (
                opt(values, "target_account_id") if t == OperationTypeCode.TRANSFER else None
            ),
            "op_date": opt(values, "op_date"),
            "name": opt(values, "name"),
            "amount": norm_money(opt(values, "amount")),
            # категория — только у расхода (у прочих типов поле скрыто)
            "category_id": opt(values, "category_id") if t == OperationTypeCode.EXPENSE else None,
            "comment": opt(values, "comment"),
            "plan_id": None if t == OperationTypeCode.TRANSFER else opt(values, "plan_id"),
        },
    )


def _category_options(db: Session, op: Operation | None) -> list[tuple[str, str]]:
    """Активные категории + отключённая, если она уже стоит у редактируемой операции."""
    cats = [c for c in references.expense_categories(db) if c.is_active]
    if op is not None and op.category is not None and not op.category.is_active:
        cats.append(op.category)
    return [(str(c.id), c.name) for c in cats]


def _render_operation_form(
    request: Request,
    db: Session,
    user: User,
    f: FormState,
    op: Operation | None = None,
    status_code: int = 200,
) -> Response:
    try:
        unallocated = accounts_svc.get_unallocated_account(db, user)
    except ConflictError:
        unallocated = None
    plans = list(plan_svc.list_plans(db, user, operation_type=OperationTypeCode.INCOME.value))
    plans += list(plan_svc.list_plans(db, user, operation_type=OperationTypeCode.EXPENSE.value))
    plans.sort(key=lambda p: (p.planned_date, p.created_at))
    return render(
        request,
        "operation_form.html",
        {
            "f": f,
            "op": op,
            "operation_types": references.operation_types(db),
            "accounts": accounts_svc.list_accounts(db, user, include_closed=False),
            "categories": _category_options(db, op),
            "unallocated": unallocated,
            "plans": plans,
        },
        status_code=status_code,
    )


@router.get("/operations/new")
def operation_new(
    request: Request,
    type: str | None = None,
    account_id: str | None = None,
    target_account_id: str | None = None,
    plan_id: uuid.UUID | None = None,
    next: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    values = {
        "operation_type": type if type in ("income", "expense", "transfer") else "expense",
        "op_date": date.today().isoformat(),
        "account_id": account_id or "",
        "target_account_id": target_account_id or "",
        "next": safe_next(next),
    }
    if plan_id is not None:
        # «Факт» из планирования: подставляем план, его дату и сумму (после налога)
        plan = plan_svc.get_plan(db, user, plan_id)
        values |= {
            "operation_type": plan.operation_type.code,
            "plan_id": str(plan.id),
            "op_date": plan.planned_date.isoformat(),
            "amount": fmt_money_input(plan.amount_net or plan.amount_planned),
            "name": plan.name or "",
        }
        if plan.funding_account_id and plan.operation_type.code == OperationTypeCode.EXPENSE:
            values["account_id"] = str(plan.funding_account_id)
    f = FormState(values=values, action="/operations/new")
    return _render_operation_form(request, db, user, f)


@router.post("/operations/new", dependencies=[Depends(csrf_protect)])
async def operation_create(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    values = await _form(request)
    f = FormState(values=values, action="/operations/new")
    try:
        ops_svc.create_operation(db, user, _operation_data(values))
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_operation_form(request, db, user, f, status_code=422)
    return redirect(safe_next(values.get("next")), flash="Операция добавлена")


@router.get("/operations/{op_id}/edit")
def operation_edit(
    request: Request,
    op_id: uuid.UUID,
    next: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    op = ops_svc.get_operation(db, user, op_id)
    values = _operation_values(op) | {"next": safe_next(next)}
    f = FormState(values=values, action=f"/operations/{op.id}/edit", edit_id=op.id)
    return _render_operation_form(request, db, user, f, op)


@router.post("/operations/{op_id}/edit", dependencies=[Depends(csrf_protect)])
async def operation_update(
    request: Request,
    op_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    op = ops_svc.get_operation(db, user, op_id)
    values = await _form(request)
    f = FormState(values=values, action=f"/operations/{op.id}/edit", edit_id=op.id)
    try:
        ops_svc.update_operation(db, user, op_id, _operation_data(values))
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_operation_form(request, db, user, f, op, status_code=422)
    return redirect(safe_next(values.get("next")), flash="Операция сохранена")


@router.post("/operations/{op_id}/delete", dependencies=[Depends(csrf_protect)])
async def operation_delete(
    request: Request,
    op_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    nxt = safe_next((await _form(request)).get("next"))
    try:
        ops_svc.delete_operation(db, user, op_id)
    except ConflictError as exc:
        db.rollback()
        return redirect(nxt, flash=exc.message, error=True)
    return redirect(nxt, flash="Операция удалена")
