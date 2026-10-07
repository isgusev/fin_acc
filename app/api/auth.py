from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app import schemas
from app.config import get_settings
from app.db import get_db
from app.deps import CSRF_COOKIE, anon_csrf_token, current_user, get_session
from app.models import User, UserSession
from app.ratelimit import limit_login, limit_register
from app.services import users as users_svc

router = APIRouter(prefix="/auth", tags=["auth"])


def set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        s.session_cookie,
        token,
        max_age=s.session_ttl_hours * 3600,
        httponly=True,
        secure=s.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        get_settings().session_cookie, path="/", secure=get_settings().cookie_secure
    )


@router.get("/csrf")
def csrf(request: Request, sess: UserSession | None = Depends(get_session)) -> dict[str, str]:
    """Токен CSRF: передавайте его в заголовке X-CSRF-Token во всех изменяющих запросах."""
    return {"csrf_token": sess.csrf_token if sess else anon_csrf_token(request)}


@router.post(
    "/register",
    response_model=schemas.UserOut,
    status_code=201,
    dependencies=[Depends(limit_register)],
)
def register(data: schemas.RegisterIn, db: Session = Depends(get_db)) -> User:
    return users_svc.register(db, data)


@router.post("/login", dependencies=[Depends(limit_login)])
def login(
    data: schemas.LoginIn, response: Response, db: Session = Depends(get_db)
) -> dict[str, object]:
    user = users_svc.authenticate(db, data.email, data.password)
    token, sess = users_svc.create_session(db, user)
    set_session_cookie(response, token)
    response.delete_cookie(CSRF_COOKIE, path="/", secure=get_settings().cookie_secure)
    return {
        "user": schemas.UserOut.model_validate(user).model_dump(mode="json"),
        "csrf_token": sess.csrf_token,
    }


@router.post("/logout", status_code=204)
def logout(
    response: Response,
    db: Session = Depends(get_db),
    sess: UserSession | None = Depends(get_session),
) -> None:
    if sess is not None:
        users_svc.destroy_session(db, sess)
    clear_session_cookie(response)


@router.get("/me", response_model=schemas.UserOut)
def me(user: User = Depends(current_user)) -> User:
    return user


@router.put("/me", response_model=schemas.UserOut)
def update_me(
    data: schemas.ProfileIn, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> User:
    return users_svc.update_profile(db, user, data)


@router.post("/password", status_code=204)
def change_password(
    data: schemas.PasswordChangeIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    sess: UserSession | None = Depends(get_session),
) -> None:
    users_svc.change_password(db, user, data, sess.id if sess else None)
