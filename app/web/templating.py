"""Jinja2-шаблоны, фильтры форматирования и общий контекст страниц."""

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


templates.env.filters["money"] = fmt_money
templates.env.filters["ru_date"] = fmt_date
templates.env.filters["period"] = fmt_period
templates.env.globals["replenish_periods"] = list(REPLENISH_PERIOD_LABELS.items())


def render(
    request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200
) -> HTMLResponse:
    """Рендер страницы с общим контекстом: текущий пользователь, CSRF-токен, flash."""
    from app.deps import anon_csrf_token

    sess = getattr(request.state, "user_session", None)
    ctx: dict[str, Any] = {
        "user": sess.user if sess else None,
        "csrf_token": sess.csrf_token if sess else anon_csrf_token(request),
        "flash": unquote(request.cookies.get(FLASH_COOKIE, ""))[:300] or None,
        "today": date.today(),
    }
    ctx.update(context or {})
    response = templates.TemplateResponse(request, name, ctx, status_code=status_code)
    if FLASH_COOKIE in request.cookies:
        response.delete_cookie(FLASH_COOKIE, path="/")
    return response


def redirect(url: str, flash: str | None = None) -> RedirectResponse:
    """PRG-редирект (303) с необязательным однократным сообщением для следующей страницы."""
    response = RedirectResponse(url, status_code=303)
    if flash:
        response.set_cookie(
            FLASH_COOKIE, quote(flash), max_age=60, httponly=True, samesite="lax", path="/"
        )
    return response


def render_error_page(request: Request, status_code: int, message: str) -> HTMLResponse:
    return render(
        request, "error.html", {"status_code": status_code, "message": message}, status_code
    )
