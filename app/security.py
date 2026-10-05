"""Хеширование паролей (Argon2id), токены сессий и CSRF."""

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.errors import ValidationAppError

_hasher = PasswordHasher()

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


# Хеш-заглушка: проверяем пароль даже для несуществующего email, чтобы время ответа
# не выдавало, зарегистрирован ли адрес.
DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def validate_password_strength(password: str, field: str = "password") -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValidationAppError(
            f"Пароль должен быть не короче {PASSWORD_MIN_LENGTH} символов", field
        )
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValidationAppError(
            f"Пароль должен быть не длиннее {PASSWORD_MAX_LENGTH} символов", field
        )
    if password.isdigit() or password.isalpha():
        raise ValidationAppError("Пароль должен содержать и буквы, и цифры (или символы)", field)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_equal(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode(), b.encode())


def generate_temp_password() -> str:
    # 12 символов: буквы и цифры, без похожих символов
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(12))
        if any(c.isdigit() for c in pwd) and any(c.isalpha() for c in pwd):
            return pwd
