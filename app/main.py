import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import api_router
from app.bodylimit import BodySizeLimitMiddleware
from app.config import get_settings
from app.db import SessionLocal
from app.deps import CSRF_COOKIE, PasswordChangeRequired
from app.errors import AppError, UnauthorizedError, humanize_pydantic_error
from app.services import users as users_svc
from app.web import web_router
from app.web.templating import STATIC_DIR, render_error_page

log = logging.getLogger("fin_acc")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.validate_for_runtime()
    with SessionLocal() as db:
        users_svc.bootstrap_admin(db)
    yield


def _is_api(request: Request) -> bool:
    return request.url.path.startswith("/api/")


def _json_error(status: int, code: str, message: str, field: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message, "field": field}}
    )


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Учёт личных финансов",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/api/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/api/openapi.json",
    )

    # ------------------------------------------------------------ middleware

    @app.middleware("http")
    async def security_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if settings.force_https and request.url.scheme != "https" and request.url.path != "/health":
            return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
        response = await call_next(request)
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        h.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'self'",
        )
        if settings.cookie_secure:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if not request.url.path.startswith("/static/"):
            h.setdefault("Cache-Control", "no-store")
        # CSRF-cookie для анонимных форм (вход/регистрация), см. deps.anon_csrf_token
        new_csrf = getattr(request.state, "new_csrf_cookie", None)
        if new_csrf:
            response.set_cookie(
                CSRF_COOKIE,
                new_csrf,
                httponly=True,
                secure=settings.cookie_secure,
                samesite="strict",
                path="/",
            )
        return response

    # ------------------------------------------------------------ ошибки

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> Response:
        if _is_api(request):
            return JSONResponse(status_code=exc.status_code, content=exc.to_dict())
        if isinstance(exc, UnauthorizedError):
            nxt = request.url.path
            return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)
        if isinstance(exc, PasswordChangeRequired):
            return RedirectResponse("/profile/password", status_code=303)
        return render_error_page(request, exc.status_code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> Response:
        errors = exc.errors()
        message, field = (
            humanize_pydantic_error(errors[0]) if errors else ("Некорректные данные", None)
        )
        if _is_api(request):
            return _json_error(422, "VALIDATION_ERROR", message, field)
        return render_error_page(request, 422, f"{field}: {message}" if field else message)

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException) -> Response:
        messages = {404: "Страница не найдена", 405: "Метод не поддерживается"}
        message = messages.get(exc.status_code, str(exc.detail))
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        if _is_api(request):
            return _json_error(exc.status_code, code, message)
        return render_error_page(request, exc.status_code, message)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> Response:
        log.exception("Необработанная ошибка: %s %s", request.method, request.url.path)
        message = "Внутренняя ошибка сервера. Попробуйте позже"
        if _is_api(request):
            return _json_error(500, "INTERNAL_ERROR", message)
        return render_error_page(request, 500, message)

    # ------------------------------------------------------------ маршруты

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.include_router(api_router)
    app.include_router(web_router)
    return app


app = create_app()
