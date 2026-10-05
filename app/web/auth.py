"""Вход, регистрация, выход, профиль и смена пароля."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app import schemas
from app.api.auth import clear_session_cookie, set_session_cookie
from app.db import get_db
from app.deps import CSRF_COOKIE, csrf_protect, current_user, get_session
from app.errors import AppError, ValidationAppError
from app.models import User, UserSession
from app.services import references
from app.services import users as users_svc
from app.web.forms import (
    FormInvalid,
    FormState,
    form_values,
    norm_money,
    opt,
    safe_next,
    validate,
)
from app.web.templating import fmt_money_input, redirect, render

router = APIRouter()


def _login_response(db: Session, user: User, nxt: str, flash: str | None = None) -> Response:
    token, _ = users_svc.create_session(db, user)
    response = redirect(nxt, flash=flash)
    set_session_cookie(response, token)
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


# ---------------------------------------------------------------- вход


@router.get("/login")
def login_page(
    request: Request,
    next: str | None = None,
    db: Session = Depends(get_db),
    sess: UserSession | None = Depends(get_session),
) -> Response:
    nxt = safe_next(next)
    if sess is not None:
        return redirect(nxt)
    f = FormState(values={"next": nxt})
    return render(
        request,
        "login.html",
        {"f": f, "registration_open": references.is_registration_open(db)},
    )


@router.post("/login", dependencies=[Depends(csrf_protect)])
async def login_submit(request: Request, db: Session = Depends(get_db)) -> Response:
    values = form_values(await request.form())
    f = FormState(values=values)
    nxt = safe_next(values.get("next"))
    try:
        data = validate(
            schemas.LoginIn,
            {"email": values.get("email", ""), "password": values.get("password", "")},
        )
        if not data.email or not data.password:
            raise FormInvalid({}, "Введите email и пароль")
        user = users_svc.authenticate(db, data.email, data.password)
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return render(
            request,
            "login.html",
            {"f": f, "registration_open": references.is_registration_open(db)},
            status_code=422,
        )
    if user.must_change_password:
        nxt = "/profile/password"
    return _login_response(db, user, nxt)


# ---------------------------------------------------------------- регистрация


@router.get("/register")
def register_page(
    request: Request,
    db: Session = Depends(get_db),
    sess: UserSession | None = Depends(get_session),
) -> Response:
    if sess is not None:
        return redirect("/")
    return render(
        request,
        "register.html",
        {"f": FormState(), "registration_open": references.is_registration_open(db)},
    )


@router.post("/register", dependencies=[Depends(csrf_protect)])
async def register_submit(request: Request, db: Session = Depends(get_db)) -> Response:
    values = form_values(await request.form())
    f = FormState(values=values)
    try:
        data = validate(
            schemas.RegisterIn,
            {
                "email": values.get("email", ""),
                "password": values.get("password", ""),
                "display_name": values.get("display_name", ""),
            },
        )
        if values.get("password") != values.get("password2"):
            raise FormInvalid({"password2": "Пароли не совпадают"})
        user = users_svc.register(db, data)
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return render(
            request,
            "register.html",
            {"f": f, "registration_open": references.is_registration_open(db)},
            status_code=422,
        )
    return _login_response(db, user, "/", flash="Добро пожаловать! Учётная запись создана")


# ---------------------------------------------------------------- выход


@router.post("/logout")
async def logout(
    request: Request,
    db: Session = Depends(get_db),
    sess: UserSession | None = Depends(get_session),
) -> Response:
    if sess is not None:
        await csrf_protect(request, sess)
        users_svc.destroy_session(db, sess)
    response = redirect("/login")
    clear_session_cookie(response)
    return response


# ---------------------------------------------------------------- профиль


def _profile_values(user: User) -> dict[str, str]:
    return {
        "display_name": user.display_name,
        "salary": fmt_money_input(user.salary),
        "advance_day": "" if user.advance_day is None else str(user.advance_day),
        "salary_day": "" if user.salary_day is None else str(user.salary_day),
        "advance_calc_day": "" if user.advance_calc_day is None else str(user.advance_calc_day),
    }


@router.get("/profile")
def profile_page(request: Request, user: User = Depends(current_user)) -> Response:
    return render(request, "profile.html", {"f": FormState(values=_profile_values(user))})


@router.post("/profile", dependencies=[Depends(csrf_protect)])
async def profile_submit(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> Response:
    values = form_values(await request.form())
    f = FormState(values=values)
    try:
        data = validate(
            schemas.ProfileIn,
            {
                "display_name": values.get("display_name", ""),
                "salary": norm_money(opt(values, "salary")),
                "advance_day": opt(values, "advance_day"),
                "salary_day": opt(values, "salary_day"),
                "advance_calc_day": opt(values, "advance_calc_day"),
            },
        )
        users_svc.update_profile(db, user, data)
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return render(request, "profile.html", {"f": f}, status_code=422)
    return redirect("/profile", flash="Профиль сохранён")


@router.get("/profile/password")
def password_page(request: Request, user: User = Depends(current_user)) -> Response:
    return render(request, "password.html", {"f": FormState()})


@router.post("/profile/password", dependencies=[Depends(csrf_protect)])
async def password_submit(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    sess: UserSession | None = Depends(get_session),
) -> Response:
    values = form_values(await request.form())
    f = FormState()
    try:
        data = validate(
            schemas.PasswordChangeIn,
            {
                "current_password": values.get("current_password", ""),
                "new_password": values.get("new_password", ""),
            },
        )
        if values.get("new_password") != values.get("new_password2"):
            raise ValidationAppError("Пароли не совпадают", "new_password2")
        users_svc.change_password(db, user, data, sess.id if sess else None)
    except (FormInvalid, AppError) as exc:
        db.rollback()
        f.apply(exc)
        return render(request, "password.html", {"f": f}, status_code=422)
    return redirect("/", flash="Пароль изменён")
