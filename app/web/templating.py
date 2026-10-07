"""Jinja2-шаблоны, фильтры форматирования и общий контекст страниц."""

import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.models import REPLENISH_PERIOD_LABELS, ReplenishPeriod

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
FLASH_COOKIE = "fa_flash"
FLASH_ERROR_PREFIX = "!"


def fmt_money(v: Decimal | int | float | None) -> str:
    """1234567.5 → «1 234 567,50» (неразрывные пробелы)."""
    if v is None:
        return ""
    s = f"{Decimal(v):,.2f}"
    return s.replace(",", " ").replace(".", ",")


def fmt_date(v: date | datetime | None) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.astimezone().strftime("%d.%m.%Y %H:%M")
    return v.strftime("%d.%m.%Y")


def fmt_period(v: ReplenishPeriod | str | None) -> str:
    if v is None:
        return ""
    return REPLENISH_PERIOD_LABELS.get(ReplenishPeriod(v), str(v))


def fmt_money_input(v: Decimal | int | float | None) -> str:
    """Значение для поля ввода суммы: 1234.5 → «1234,50»."""
    if v is None:
        return ""
    return f"{Decimal(v):.2f}".replace(".", ",")


def fmt_rate(v: Decimal | int | float | None) -> str:
    """Ставка без лишних нулей: 13.00 → «13», 13.50 → «13,5»."""
    if v is None:
        return ""
    s = f"{Decimal(v):f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",")


def plan_label(p: Any) -> str:
    """Подпись записи планирования: «10.01.2026 · Зарплата · 191 400,00»."""
    if p is None:
        return ""
    title = p.name or (p.income_kind.name if p.income_kind else None) or p.operation_type.name
    return f"{fmt_date(p.planned_date)} · {title} · {fmt_money(p.amount_planned)}"


def fmt_date_iso(v: str) -> str:
    """«2026-09-28» → «28.09.2026» (значения скрытых полей формы — строки)."""
    try:
        return fmt_date(date.fromisoformat(v))
    except (TypeError, ValueError):
        return v


def fmt_money_str(v: str) -> str:
    try:
        return fmt_money(Decimal(v))
    except (ArithmeticError, TypeError, ValueError):
        return v


def fmt_month(v: date) -> str:
    """date(2026, 12, 1) → «декабре 2026» (предложный падеж для «в …»)."""
    names = [
        "январе",
        "феврале",
        "марте",
        "апреле",
        "мае",
        "июне",
        "июле",
        "августе",
        "сентябре",
        "октябре",
        "ноябре",
        "декабре",
    ]
    return f"{names[v.month - 1]} {v.year}"


templates.env.filters["money"] = fmt_money
templates.env.filters["ru_month"] = fmt_month
templates.env.filters["ru_date_iso"] = fmt_date_iso
templates.env.filters["money_str"] = fmt_money_str
templates.env.filters["money_input"] = fmt_money_input
templates.env.filters["rate"] = fmt_rate
templates.env.filters["plan_label"] = plan_label
templates.env.filters["ru_date"] = fmt_date
templates.env.filters["period"] = fmt_period
templates.env.globals["replenish_periods"] = list(REPLENISH_PERIOD_LABELS.items())


def _static_version() -> str:
    """Хеш содержимого статики: меняется при обновлении — браузер не берёт старый JS/CSS из кеша."""
    h = hashlib.sha256()
    for name in ("app.js", "app.css"):
        path = STATIC_DIR / name
        if path.exists():
            h.update(path.read_bytes())
    return h.hexdigest()[:12]


templates.env.globals["static_version"] = _static_version()


def render(
    request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200
) -> HTMLResponse:
    """Рендер страницы с общим контекстом: текущий пользователь, CSRF-токен, flash."""
    from app.deps import anon_csrf_token

    sess = getattr(request.state, "user_session", None)
    flash = unquote(request.cookies.get(FLASH_COOKIE, ""))[:300] or None
    flash_error = bool(flash and flash.startswith(FLASH_ERROR_PREFIX))
    if flash and flash_error:
        flash = flash.removeprefix(FLASH_ERROR_PREFIX) or None
    ctx: dict[str, Any] = {
        "user": sess.user if sess else None,
        "csrf_token": sess.csrf_token if sess else anon_csrf_token(request),
        "flash": flash,
        "flash_error": flash_error,
        "today": date.today(),
        "path": request.url.path,
        "current_url": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
    }
    ctx.update(context or {})
    response = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    if FLASH_COOKIE in request.cookies:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response


def redirect(url: str, flash: str | None = None, error: bool = False) -> RedirectResponse:
    """PRG-редирект (303) с необязательным однократным сообщением для следующей страницы.

    error=True — сообщение показывается как ошибка.
    """
    response = RedirectResponse(url, status_code=303)
    if flash:
        if error:
            flash = FLASH_ERROR_PREFIX + flash
        response.set_cookie(
            FLASH_COOKIE, quote(flash), max_age=60, httponly=True, samesite="lax", path="/"
        )
    return response


def render_error_page(request: Request, status_code: int, message: str) -> HTMLResponse:
    return render(
        request, "error.html", {"status_code": status_code, "message": message}, status_code
    )
