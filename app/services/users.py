"""Пользователи: регистрация, вход, сессии, профиль, администрирование."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import schemas
from app.config import get_settings
from app.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    UnauthorizedError,
    ValidationAppError,
)
from app.models import User, UserRole, UserSession
from app.security import (
    DUMMY_HASH,
    generate_temp_password,
    hash_password,
    new_token,
    password_needs_rehash,
    token_hash,
    validate_password_strength,
    verify_password,
)
from app.services import accounts as accounts_svc
from app.services import references

INVALID_CREDENTIALS = "Неверный email или пароль"


def _now() -> datetime:
    return datetime.now(UTC)


def get_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))


def create_user(
    db: Session,
    email: str,
    password: str,
    display_name: str,
    role: UserRole = UserRole.USER,
) -> User:
    validate_password_strength(password)
    if get_by_email(db, email) is not None:
        raise ConflictError("Пользователь с таким email уже зарегистрирован", "email")
    user = User(
        email=email.strip().lower(),
        password_hash=hash_password(password),
        display_name=display_name,
        role=role,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise ConflictError("Пользователь с таким email уже зарегистрирован", "email") from e
    accounts_svc.ensure_unallocated_account(db, user)  # коммитит
    return user


def register(db: Session, data: schemas.RegisterIn) -> User:
    if not references.is_registration_open(db):
        raise ForbiddenError("Регистрация новых пользователей закрыта администратором")
    return create_user(db, data.email, data.password, data.display_name)


def authenticate(db: Session, email: str, password: str) -> User:
    s = get_settings()
    user = get_by_email(db, email)
    if user is None:
        verify_password(DUMMY_HASH, password)  # выравниваем время ответа
        raise UnauthorizedError(INVALID_CREDENTIALS)
    if user.locked_until and user.locked_until > _now():
        raise RateLimitedError("Слишком много неудачных попыток входа. Попробуйте позже")
    if not verify_password(user.password_hash, password):
        user.failed_login_count += 1
        if user.failed_login_count >= s.login_max_attempts:
            user.locked_until = _now() + timedelta(minutes=s.login_lock_minutes)
            user.failed_login_count = 0
        db.commit()
        raise UnauthorizedError(INVALID_CREDENTIALS)
    if not user.is_active:
        raise ForbiddenError("Учётная запись заблокирована. Обратитесь к администратору")
    user.failed_login_count = 0
    user.locked_until = None
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    db.commit()
    return user


def create_session(db: Session, user: User) -> tuple[str, UserSession]:
    """Создаёт сессию; возвращает (токен для cookie, сессия)."""
    token = new_token()
    sess = UserSession(
        token_hash=token_hash(token),
        csrf_token=new_token(),
        user_id=user.id,
        expires_at=_now() + timedelta(hours=get_settings().session_ttl_hours),
    )
    db.add(sess)
    # заодно чистим просроченные сессии пользователя
    db.execute(
        delete(UserSession).where(UserSession.user_id == user.id, UserSession.expires_at < _now())
    )
    db.commit()
    return token, sess


def session_by_token(db: Session, token: str | None) -> UserSession | None:
    if not token:
        return None
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
    if sess is None or sess.expires_at < _now() or not sess.user.is_active:
        return None
    return sess


def destroy_session(db: Session, sess: UserSession) -> None:
    db.delete(sess)
    db.commit()


def destroy_all_sessions(
    db: Session, user_id: uuid.UUID, except_id: uuid.UUID | None = None
) -> None:
    q = delete(UserSession).where(UserSession.user_id == user_id)
    if except_id is not None:
        q = q.where(UserSession.id != except_id)
    db.execute(q)


def update_profile(db: Session, user: User, data: schemas.ProfileIn) -> User:
    for k, v in data.model_dump().items():
        setattr(user, k, v)
    db.commit()
    return user


def change_password(
    db: Session, user: User, data: schemas.PasswordChangeIn, current_session_id: uuid.UUID | None
) -> None:
    if not verify_password(user.password_hash, data.current_password):
        raise ValidationAppError("Текущий пароль указан неверно", "current_password")
    validate_password_strength(data.new_password, "new_password")
    if data.new_password == data.current_password:
        raise ValidationAppError("Новый пароль должен отличаться от текущего", "new_password")
    user.password_hash = hash_password(data.new_password)
    user.must_change_password = False
    destroy_all_sessions(db, user.id, except_id=current_session_id)
    db.commit()


# ---------------------------------------------------------------- администрирование


def list_users(db: Session) -> Sequence[User]:
    return db.scalars(select(User).order_by(User.created_at)).all()


def get_user(db: Session, user_id: uuid.UUID) -> User:
    u = db.get(User, user_id)
    if u is None:
        raise NotFoundError("Пользователь не найден")
    return u


def admin_reset_password(db: Session, admin: User, user_id: uuid.UUID) -> str:
    """Сбрасывает пароль на временный; пользователь обязан сменить его при входе."""
    u = get_user(db, user_id)
    if u.id == admin.id:
        raise ConflictError("Свой пароль меняйте в профиле, а не через сброс")
    temp = generate_temp_password()
    u.password_hash = hash_password(temp)
    u.must_change_password = True
    u.failed_login_count = 0
    u.locked_until = None
    destroy_all_sessions(db, u.id)
    db.commit()
    return temp


def _admins_count(db: Session) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.role == UserRole.ADMIN, User.is_active)
        )
        or 0
    )


def admin_set_active(db: Session, admin: User, user_id: uuid.UUID, active: bool) -> User:
    u = get_user(db, user_id)
    if u.id == admin.id and not active:
        raise ConflictError("Нельзя заблокировать самого себя")
    u.is_active = active
    if not active:
        destroy_all_sessions(db, u.id)
    db.commit()
    return u


def admin_set_role(db: Session, admin: User, user_id: uuid.UUID, role: UserRole) -> User:
    u = get_user(db, user_id)
    if u.role == UserRole.ADMIN and role != UserRole.ADMIN and _admins_count(db) <= 1:
        raise ConflictError("Нельзя снять роль с последнего администратора")
    u.role = role
    db.commit()
    return u


def bootstrap_admin(db: Session) -> None:
    """Создаёт первого администратора из ADMIN_EMAIL / ADMIN_PASSWORD, если админов ещё нет."""
    s = get_settings()
    if not s.admin_email or not s.admin_password:
        return
    if db.scalar(select(User.id).where(User.role == UserRole.ADMIN).limit(1)) is not None:
        return
    existing = get_by_email(db, s.admin_email)
    if existing is not None:
        existing.role = UserRole.ADMIN
        db.commit()
        return
    try:
        create_user(
            db, s.admin_email, s.admin_password.get_secret_value(), "Администратор", UserRole.ADMIN
        )
    except ConflictError:
        # Несколько воркеров стартуют одновременно — администратора уже создал другой
        db.rollback()
