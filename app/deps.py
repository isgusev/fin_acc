"""Зависимости FastAPI: текущий пользователь, права администратора, CSRF."""

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import CsrfError, ForbiddenError, UnauthorizedError
from app.models import User, UserSession
from app.security import new_token, tokens_equal
from app.services import users as users_svc

CSRF_COOKIE = get_settings().csrf_cookie
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Пути, доступные пользователю с временным паролем (до его смены)
PASSWORD_CHANGE_ALLOWED = {
    "/profile/password",
    "/logout",
    "/api/v1/auth/password",
    "/api/v1/auth/logout",
}
# ...и только на чтение
PASSWORD_CHANGE_READ_ONLY = {"/api/v1/auth/me"}


class PasswordChangeRequired(ForbiddenError):
    code = "PASSWORD_CHANGE_REQUIRED"


def get_session(request: Request, db: Session = Depends(get_db)) -> UserSession | None:
    if hasattr(request.state, "user_session"):
        return request.state.user_session
    token = request.cookies.get(get_settings().session_cookie)
    sess = users_svc.session_by_token(db, token)
    request.state.user_session = sess
    return sess


def anon_csrf_token(request: Request) -> str:
    """CSRF-токен для анонимных форм (double-submit cookie). Ставится в cookie middleware."""
    token = request.cookies.get(CSRF_COOKIE) or getattr(request.state, "new_csrf_cookie", None)
    if not token:
        token = new_token()
        request.state.new_csrf_cookie = token
    return token


def csrf_token_for(request: Request, sess: UserSession | None) -> str:
    return sess.csrf_token if sess else anon_csrf_token(request)


async def csrf_protect(request: Request, sess: UserSession | None = Depends(get_session)) -> None:
    """Проверка CSRF для небезопасных методов: токен из заголовка или поля формы."""
    if request.method in SAFE_METHODS:
        return
    sent = request.headers.get(CSRF_HEADER)
    if not sent:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            value = form.get(CSRF_FORM_FIELD)
            sent = value if isinstance(value, str) else None
    expected = sess.csrf_token if sess else request.cookies.get(CSRF_COOKIE)
    if not tokens_equal(sent, expected):
        raise CsrfError("Сессия формы устарела. Обновите страницу и повторите действие")


def current_user(request: Request, sess: UserSession | None = Depends(get_session)) -> User:
    if sess is None:
        raise UnauthorizedError("Требуется вход в систему")
    user = sess.user
    path = request.url.path
    allowed = path in PASSWORD_CHANGE_ALLOWED or (
        path in PASSWORD_CHANGE_READ_ONLY and request.method in SAFE_METHODS
    )
    if user.must_change_password and not allowed:
        raise PasswordChangeRequired("Необходимо сменить временный пароль")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise ForbiddenError("Доступно только администратору")
    return user
