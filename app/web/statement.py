"""«Импорт выписки»: вставка текста выписки Т-Банка → таблица проверки → расходы."""

import re
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app import schemas
from app.db import get_db
from app.deps import csrf_protect, current_user
from app.errors import AppError, RowsValidationError
from app.models import OperationTypeCode, User
from app.services import accounts as accounts_svc
from app.services import planning as plan_svc
from app.services import references
from app.services import statement_import as import_svc
from app.web.forms import FormInvalid, FormState, form_values, norm_money, opt, validate
from app.web.templating import redirect, render

router = APIRouter()

ROW_FIELD = re.compile(r"^rows-(\d+)-(\w+)$")
EDITABLE = ("name", "account_id", "category_id", "plan_id", "comment")
FIXED = ("op_date", "amount", "description", "card")


def _choices(db: Session, user: User, years: set[int]) -> dict[str, Any]:
    plans = [
        p
        for y in sorted(years)
        for p in plan_svc.list_plans(db, user, year=y, operation_type=OperationTypeCode.EXPENSE)
    ]
    return {
        "accounts": accounts_svc.list_accounts(db, user, include_closed=False),
        "categories": references.expense_categories(db, only_active=True),
        "plans": plans,
    }


def _render_table(
    request: Request,
    db: Session,
    user: User,
    rows: list[dict[str, Any]],
    info: dict[str, int] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    years = {date.fromisoformat(r["op_date"]).year for r in rows} or {date.today().year}
    return render(
        request,
        "import_table.html",
        {"rows": rows, "info": info, "error": error, **_choices(db, user, years)},
        status_code=status_code,
    )


@router.get("/import")
def import_page(request: Request, user: User = Depends(current_user)) -> Response:
    return render(request, "import_text.html", {"f": FormState(action="/import/parse")})


@router.post("/import/parse", dependencies=[Depends(csrf_protect)])
async def import_parse(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    values = form_values(await request.form())
    f = FormState(values=values, action="/import/parse")
    try:
        data = validate(schemas.ImportTextIn, {"text": values.get("text", "")})
        prepared = import_svc.prepare(db, user, data.text)
    except (FormInvalid, AppError) as exc:
        f.apply(exc)
        return render(request, "import_text.html", {"f": f}, status_code=422)
    rows = [
        {
            "op_date": r.op_date.isoformat(),
            "amount": str(r.amount),
            "description": r.description,
            "card": r.card or "",
            "name": r.suggestion.name or "",
            "account_id": str(r.suggestion.account_id or ""),
            "category_id": str(r.suggestion.category_id or ""),
            "plan_id": "",
            "comment": "",
            "source": r.suggestion.source or "",
            "errors": {},
        }
        for r in prepared.rows
    ]
    info = {
        "total": prepared.total_lines,
        "shown": len(rows),
        "income": prepared.skipped_income,
        "duplicates": prepared.skipped_duplicates,
        "unparsed": prepared.unparsed,
    }
    return _render_table(request, db, user, rows, info)


def _rows_from_form(values: dict[str, str]) -> list[dict[str, Any]]:
    """Строки таблицы из полей вида rows-<n>-<поле> (удалённые строки в форме отсутствуют)."""
    by_index: dict[int, dict[str, Any]] = {}
    for key, value in values.items():
        m = ROW_FIELD.match(key)
        if m and m.group(2) in (*EDITABLE, *FIXED, "source"):
            by_index.setdefault(int(m.group(1)), {})[m.group(2)] = value
    rows = []
    for i in sorted(by_index):
        row = {k: by_index[i].get(k, "") for k in (*EDITABLE, *FIXED, "source")}
        row["errors"] = {}
        rows.append(row)
    return rows


@router.post("/import/save", dependencies=[Depends(csrf_protect)])
async def import_save(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    rows = _rows_from_form(form_values(await request.form()))
    if not rows:
        return redirect("/import", flash="Все строки удалены — сохранять нечего", error=True)
    parsed: list[schemas.ImportRowIn] = []
    has_errors = False
    for row in rows:
        try:
            parsed.append(
                validate(
                    schemas.ImportRowIn,
                    {
                        "op_date": opt(row, "op_date"),
                        "amount": norm_money(opt(row, "amount")),
                        "description": opt(row, "description"),
                        **{k: opt(row, k) for k in ("card", *EDITABLE)},
                    },
                )
            )
        except FormInvalid as exc:
            row["errors"] = exc.errors
            has_errors = True
    if not has_errors:
        try:
            created = import_svc.save(db, user, parsed)
        except RowsValidationError as exc:
            db.rollback()
            for i, errs in exc.row_errors.items():
                rows[i]["errors"] = {(f or "general"): m for f, m in errs}
            has_errors = True
        except AppError as exc:
            db.rollback()
            return _render_table(request, db, user, rows, error=exc.message, status_code=422)
    if has_errors:
        bad = sum(1 for r in rows if r["errors"])
        return _render_table(
            request,
            db,
            user,
            rows,
            error=f"Ничего не сохранено: исправьте ошибки в строках ({bad})",
            status_code=422,
        )
    return redirect("/", flash=f"Из выписки добавлено расходов: {len(created)}")
