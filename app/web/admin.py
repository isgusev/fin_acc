"""Раздел администратора: пользователи, настройки, справочники, налоги, праздники."""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app import schemas
from app.db import get_db
from app.deps import admin_user, csrf_protect
from app.errors import AppError, ConflictError, NotFoundError
from app.models import AccountType, IncomeKind, OperationType, User, UserRole
from app.services import references
from app.services import users as users_svc
from app.web.forms import FormInvalid, FormState, checkbox, form_values, norm_money, opt, validate
from app.web.templating import fmt_money_input, fmt_rate, redirect, render

router = APIRouter(prefix="/admin", dependencies=[Depends(admin_user)])
csrf = [Depends(csrf_protect)]


async def _form(request: Request) -> dict[str, str]:
    return form_values(await request.form())


def _int_or_none(raw: str | None) -> int | None:
    return int(raw) if raw and raw.isdigit() else None


# ---------------------------------------------------------------- пользователи и настройки


def _render_users(
    request: Request, db: Session, temp_password: str | None = None, target: User | None = None
) -> Response:
    return render(
        request,
        "admin/users.html",
        {
            "users": users_svc.list_users(db),
            "registration_open": references.is_registration_open(db),
            "temp_password": temp_password,
            "target": target,
            "tab": "users",
        },
    )


@router.get("")
def admin_index(request: Request, db: Session = Depends(get_db)) -> Response:
    return _render_users(request, db)


@router.post("/users/{user_id}/reset-password", dependencies=csrf)
def admin_reset_password(
    request: Request,
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
) -> Response:
    temp = users_svc.admin_reset_password(db, admin, user_id)
    target = users_svc.get_user(db, user_id)
    # Временный пароль показывается один раз прямо в ответе (не в cookie и не в URL)
    return _render_users(request, db, temp_password=temp, target=target)


@router.post("/users/{user_id}/active", dependencies=csrf)
async def admin_set_active(
    request: Request,
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
) -> Response:
    active = checkbox(await _form(request), "is_active")
    try:
        u = users_svc.admin_set_active(db, admin, user_id, active)
    except ConflictError as exc:
        db.rollback()
        return redirect("/admin", flash=exc.message, error=True)
    state = "разблокирован" if active else "заблокирован"
    return redirect("/admin", flash=f"Пользователь {u.email} {state}")


@router.post("/users/{user_id}/role", dependencies=csrf)
async def admin_set_role(
    request: Request,
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
) -> Response:
    raw = (await _form(request)).get("role", "")
    try:
        role = UserRole(raw)
    except ValueError:
        return redirect("/admin", flash="Недопустимая роль", error=True)
    try:
        u = users_svc.admin_set_role(db, admin, user_id, role)
    except ConflictError as exc:
        db.rollback()
        return redirect("/admin", flash=exc.message, error=True)
    label = "администратор" if role == UserRole.ADMIN else "пользователь"
    return redirect("/admin", flash=f"Роль {u.email}: {label}")


@router.post("/settings", dependencies=csrf)
async def admin_settings(request: Request, db: Session = Depends(get_db)) -> Response:
    values = await _form(request)
    data = schemas.AppSettingsIn(registration_open=checkbox(values, "registration_open"))
    references.update_settings(db, data)
    msg = "Регистрация открыта" if data.registration_open else "Регистрация закрыта"
    return redirect("/admin", flash=msg)


# ---------------------------------------------------------------- справочники


def _at_values(at: AccountType) -> dict[str, str]:
    return {
        "name": at.name,
        "single_per_user": "1" if at.single_per_user else "",
        "sort_order": str(at.sort_order),
    }


def _ik_values(ik: IncomeKind) -> dict[str, str]:
    return {
        "name": ik.name,
        "sort_order": str(ik.sort_order),
        "is_active": "1" if ik.is_active else "",
    }


def _ot_values(ot: OperationType) -> dict[str, str]:
    return {"name": ot.name}


def _render_references(
    request: Request,
    db: Session,
    forms: dict[str, FormState] | None = None,
    status_code: int = 200,
) -> Response:
    forms = forms or {}
    at_form = forms.get("at") or FormState(
        values={"sort_order": "100"}, action="/admin/account-types"
    )
    ik_form = forms.get("ik") or FormState(
        values={"sort_order": "100", "is_active": "1"}, action="/admin/income-kinds"
    )
    ot_form = forms.get("ot")
    edit_at = db.get(AccountType, at_form.edit_id) if at_form.edit_id else None
    return render(
        request,
        "admin/references.html",
        {
            "account_types": references.account_types(db),
            "operation_types": references.operation_types(db),
            "income_kinds": references.income_kinds(db),
            "at_form": at_form,
            "ik_form": ik_form,
            "ot_form": ot_form,
            "edit_at": edit_at,
            "edit_ik": db.get(IncomeKind, ik_form.edit_id) if ik_form.edit_id else None,
            "tab": "references",
        },
        status_code=status_code,
    )


@router.get("/references")
def admin_references(
    request: Request,
    at: str | None = None,
    ik: str | None = None,
    ot: str | None = None,
    db: Session = Depends(get_db),
) -> Response:
    forms: dict[str, FormState] = {}
    if (at_id := _int_or_none(at)) is not None:
        obj = references.get_account_type(db, at_id)
        forms["at"] = FormState(
            values=_at_values(obj), action=f"/admin/account-types/{obj.id}", edit_id=obj.id
        )
    if (ik_id := _int_or_none(ik)) is not None:
        kind = references.get_income_kind(db, ik_id)
        forms["ik"] = FormState(
            values=_ik_values(kind), action=f"/admin/income-kinds/{kind.id}", edit_id=kind.id
        )
    if (ot_id := _int_or_none(ot)) is not None:
        optype = db.get(OperationType, ot_id)
        if optype is None:
            raise NotFoundError("Тип операции не найден")
        forms["ot"] = FormState(
            values=_ot_values(optype),
            action=f"/admin/operation-types/{optype.id}",
            edit_id=optype.id,
        )
    return _render_references(request, db, forms)


def _at_data(values: dict[str, str], locked: AccountType | None) -> schemas.AccountTypeIn:
    single = locked.single_per_user if locked else checkbox(values, "single_per_user")
    return validate(
        schemas.AccountTypeIn,
        {
            "name": values.get("name", ""),
            "single_per_user": single,
            "sort_order": opt(values, "sort_order"),
        },
    )


@router.post("/account-types", dependencies=csrf)
async def account_type_create(request: Request, db: Session = Depends(get_db)) -> Response:
    values = await _form(request)
    f = FormState(values=values, action="/admin/account-types")
    try:
        at = references.create_account_type(db, _at_data(values, None))
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_references(request, db, {"at": f}, 422)
    return redirect("/admin/references", flash=f"Тип счёта «{at.name}» добавлен")


@router.post("/account-types/{type_id}", dependencies=csrf)
async def account_type_update(
    request: Request, type_id: int, db: Session = Depends(get_db)
) -> Response:
    current = references.get_account_type(db, type_id)
    locked = current if current.code is not None else None
    values = await _form(request)
    f = FormState(values=values, action=f"/admin/account-types/{type_id}", edit_id=type_id)
    try:
        at = references.update_account_type(db, type_id, _at_data(values, locked))
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_references(request, db, {"at": f}, 422)
    return redirect("/admin/references", flash=f"Тип счёта «{at.name}» сохранён")


@router.post("/account-types/{type_id}/delete", dependencies=csrf)
def account_type_delete(type_id: int, db: Session = Depends(get_db)) -> Response:
    try:
        references.delete_account_type(db, type_id)
    except ConflictError as exc:
        db.rollback()
        return redirect("/admin/references", flash=exc.message, error=True)
    return redirect("/admin/references", flash="Тип счёта удалён")


@router.post("/operation-types/{type_id}", dependencies=csrf)
async def operation_type_update(
    request: Request, type_id: int, db: Session = Depends(get_db)
) -> Response:
    values = await _form(request)
    f = FormState(values=values, action=f"/admin/operation-types/{type_id}", edit_id=type_id)
    try:
        data = validate(schemas.OperationTypeIn, {"name": values.get("name", "")})
        ot = references.update_operation_type(db, type_id, data)
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_references(request, db, {"ot": f}, 422)
    return redirect("/admin/references", flash=f"Тип операции «{ot.name}» сохранён")


def _ik_data(values: dict[str, str], system: bool) -> schemas.IncomeKindIn:
    return validate(
        schemas.IncomeKindIn,
        {
            "name": values.get("name", ""),
            "sort_order": opt(values, "sort_order"),
            "is_active": True if system else checkbox(values, "is_active"),
        },
    )


@router.post("/income-kinds", dependencies=csrf)
async def income_kind_create(request: Request, db: Session = Depends(get_db)) -> Response:
    values = await _form(request)
    f = FormState(values=values, action="/admin/income-kinds")
    try:
        ik = references.create_income_kind(db, _ik_data(values, False))
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_references(request, db, {"ik": f}, 422)
    return redirect("/admin/references", flash=f"Вид дохода «{ik.name}» добавлен")


@router.post("/income-kinds/{kind_id}", dependencies=csrf)
async def income_kind_update(
    request: Request, kind_id: int, db: Session = Depends(get_db)
) -> Response:
    current = references.get_income_kind(db, kind_id)
    values = await _form(request)
    f = FormState(values=values, action=f"/admin/income-kinds/{kind_id}", edit_id=kind_id)
    try:
        ik = references.update_income_kind(db, kind_id, _ik_data(values, current.code is not None))
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_references(request, db, {"ik": f}, 422)
    return redirect("/admin/references", flash=f"Вид дохода «{ik.name}» сохранён")


@router.post("/income-kinds/{kind_id}/toggle", dependencies=csrf)
def income_kind_toggle(kind_id: int, db: Session = Depends(get_db)) -> Response:
    ik = references.get_income_kind(db, kind_id)
    data = schemas.IncomeKindIn(name=ik.name, sort_order=ik.sort_order, is_active=not ik.is_active)
    try:
        ik = references.update_income_kind(db, kind_id, data)
    except AppError as exc:
        db.rollback()
        return redirect("/admin/references", flash=exc.message, error=True)
    state = "включён" if ik.is_active else "отключён"
    return redirect("/admin/references", flash=f"Вид дохода «{ik.name}» {state}")


@router.post("/income-kinds/{kind_id}/delete", dependencies=csrf)
def income_kind_delete(kind_id: int, db: Session = Depends(get_db)) -> Response:
    try:
        references.delete_income_kind(db, kind_id)
    except ConflictError as exc:
        db.rollback()
        return redirect("/admin/references", flash=exc.message, error=True)
    return redirect("/admin/references", flash="Вид дохода удалён")


# ---------------------------------------------------------------- шкала налогов


def _render_tax(
    request: Request, db: Session, f: FormState | None = None, status_code: int = 200
) -> Response:
    f = f or FormState(action="/admin/tax")
    return render(
        request,
        "admin/tax.html",
        {"brackets": references.tax_brackets(db), "f": f, "tab": "tax"},
        status_code=status_code,
    )


@router.get("/tax")
def admin_tax(request: Request, edit: str | None = None, db: Session = Depends(get_db)) -> Response:
    f = None
    if (bid := _int_or_none(edit)) is not None:
        b = references.get_tax_bracket(db, bid)
        f = FormState(
            values={
                "income_from": fmt_money_input(b.income_from),
                "income_to": fmt_money_input(b.income_to),
                "rate": fmt_rate(b.rate),
            },
            action=f"/admin/tax/{b.id}",
            edit_id=b.id,
        )
    return _render_tax(request, db, f)


def _tax_data(values: dict[str, str]) -> schemas.TaxBracketIn:
    return validate(
        schemas.TaxBracketIn,
        {
            "income_from": norm_money(opt(values, "income_from")),
            "income_to": norm_money(opt(values, "income_to")),
            "rate": norm_money(opt(values, "rate")),
        },
    )


@router.post("/tax", dependencies=csrf)
async def tax_create(request: Request, db: Session = Depends(get_db)) -> Response:
    values = await _form(request)
    f = FormState(values=values, action="/admin/tax")
    try:
        references.create_tax_bracket(db, _tax_data(values))
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_tax(request, db, f, 422)
    return redirect("/admin/tax", flash="Ступень шкалы добавлена")


@router.post("/tax/{bracket_id}", dependencies=csrf)
async def tax_update(request: Request, bracket_id: int, db: Session = Depends(get_db)) -> Response:
    references.get_tax_bracket(db, bracket_id)
    values = await _form(request)
    f = FormState(values=values, action=f"/admin/tax/{bracket_id}", edit_id=bracket_id)
    try:
        references.update_tax_bracket(db, bracket_id, _tax_data(values))
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_tax(request, db, f, 422)
    return redirect("/admin/tax", flash="Ступень шкалы сохранена")


@router.post("/tax/{bracket_id}/delete", dependencies=csrf)
def tax_delete(bracket_id: int, db: Session = Depends(get_db)) -> Response:
    references.delete_tax_bracket(db, bracket_id)
    return redirect("/admin/tax", flash="Ступень шкалы удалена")


# ---------------------------------------------------------------- праздничные даты


def _render_holidays(
    request: Request, db: Session, f: FormState | None = None, status_code: int = 200
) -> Response:
    f = f or FormState(action="/admin/holidays")
    return render(
        request,
        "admin/holidays.html",
        {"holidays": references.holidays(db, from_year=date.today().year), "f": f, "tab": "hol"},
        status_code=status_code,
    )


@router.get("/holidays")
def admin_holidays(
    request: Request, edit: str | None = None, db: Session = Depends(get_db)
) -> Response:
    f = None
    if (hid := _int_or_none(edit)) is not None:
        h = references.get_holiday(db, hid)
        f = FormState(
            values={
                "date_from": h.date_from.isoformat(),
                "date_to": h.date_to.isoformat(),
                "name": h.name,
            },
            action=f"/admin/holidays/{h.id}",
            edit_id=h.id,
        )
    return _render_holidays(request, db, f)


def _holiday_data(values: dict[str, str]) -> schemas.HolidayIn:
    return validate(
        schemas.HolidayIn,
        {
            "date_from": opt(values, "date_from"),
            "date_to": opt(values, "date_to"),  # пусто → равна «Дате от»
            "name": values.get("name", ""),
        },
    )


@router.post("/holidays", dependencies=csrf)
async def holiday_create(request: Request, db: Session = Depends(get_db)) -> Response:
    values = await _form(request)
    f = FormState(values=values, action="/admin/holidays")
    try:
        references.create_holiday(db, _holiday_data(values))
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_holidays(request, db, f, 422)
    return redirect("/admin/holidays", flash="Праздничная дата добавлена")


@router.post("/holidays/{holiday_id}", dependencies=csrf)
async def holiday_update(
    request: Request, holiday_id: int, db: Session = Depends(get_db)
) -> Response:
    references.get_holiday(db, holiday_id)
    values = await _form(request)
    f = FormState(values=values, action=f"/admin/holidays/{holiday_id}", edit_id=holiday_id)
    try:
        references.update_holiday(db, holiday_id, _holiday_data(values))
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return _render_holidays(request, db, f, 422)
    return redirect("/admin/holidays", flash="Праздничная дата сохранена")


@router.post("/holidays/{holiday_id}/delete", dependencies=csrf)
def holiday_delete(holiday_id: int, db: Session = Depends(get_db)) -> Response:
    references.delete_holiday(db, holiday_id)
    return redirect("/admin/holidays", flash="Праздничная дата удалена")
