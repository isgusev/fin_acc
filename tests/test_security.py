"""Пароли, cookie сессии, заголовки безопасности, /health."""

import pytest

from app.errors import ValidationAppError
from app.security import (
    generate_temp_password,
    hash_password,
    token_hash,
    tokens_equal,
    validate_password_strength,
    verify_password,
)
from tests.conftest import API, PASSWORD, anon_csrf

# ---------------------------------------------------------------- пароли


def test_hash_is_argon2_and_salted():
    h1 = hash_password("Secret-pass1")
    h2 = hash_password("Secret-pass1")
    assert h1.startswith("$argon2id$")
    assert h1 != h2
    assert "Secret-pass1" not in h1


def test_verify_password():
    h = hash_password("Secret-pass1")
    assert verify_password(h, "Secret-pass1") is True
    assert verify_password(h, "secret-pass1") is False
    assert verify_password(h, "") is False


def test_verify_against_garbage_hash_is_false():
    assert verify_password("not-a-hash", "Secret-pass1") is False


def test_user_password_stored_hashed(user):
    assert user.password_hash != PASSWORD
    assert verify_password(user.password_hash, PASSWORD)


@pytest.mark.parametrize("pwd", ["Abc12", "1234567890", "OnlyLetters", "Пароль", "a1" * 65])
def test_weak_passwords_rejected(pwd):
    with pytest.raises(ValidationAppError) as ei:
        validate_password_strength(pwd)
    assert ei.value.field == "password"


@pytest.mark.parametrize("pwd", ["abcdefg1", "Secret-pass1", "пароль-с-символами", "a1" * 64])
def test_strong_passwords_accepted(pwd):
    validate_password_strength(pwd)


def test_password_strength_custom_field():
    with pytest.raises(ValidationAppError) as ei:
        validate_password_strength("short", "new_password")
    assert ei.value.field == "new_password"


def test_temp_password_is_strong():
    for _ in range(20):
        validate_password_strength(generate_temp_password())


def test_token_helpers():
    assert len(token_hash("abc")) == 64
    assert tokens_equal("abc", "abc")
    assert not tokens_equal("abc", "abd")
    assert not tokens_equal(None, None)
    assert not tokens_equal("", "")


# ---------------------------------------------------------------- HTTP


def _cookie_header(response, name):
    for value in response.headers.get_list("set-cookie"):
        if value.startswith(f"{name}="):
            return value.lower()
    raise AssertionError(f"cookie {name} не установлена")


def test_session_cookie_flags(client, user, db):
    r = client.post(
        f"{API}/auth/login",
        json={"email": user.email, "password": PASSWORD},
        headers={"X-CSRF-Token": anon_csrf(client)},
    )
    assert r.status_code == 200
    cookie = _cookie_header(r, "fa_session")
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    # в cookie лежит токен, в БД — только его хеш
    token = r.cookies.get("fa_session") or client.cookies.get("fa_session")
    from app.models import UserSession

    sess = db.query(UserSession).filter_by(user_id=user.id).one()
    assert sess.token_hash == token_hash(token)
    assert sess.token_hash != token


def test_anon_csrf_cookie_flags(client):
    r = client.get(f"{API}/auth/csrf")
    cookie = _cookie_header(r, "fa_csrf")
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


def test_health_and_security_headers(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "same-origin"
    assert r.headers["Cache-Control"] == "no-store"


def test_security_headers_on_api_errors(client):
    r = client.get(f"{API}/accounts")
    assert r.status_code == 401
    assert "Content-Security-Policy" in r.headers
    assert r.headers["X-Frame-Options"] == "DENY"


def test_api_error_does_not_leak_internals(user_client):
    r = user_client.get(f"{API}/accounts/not-a-uuid")
    assert r.status_code == 422
    body = r.text.lower()
    assert "traceback" not in body
    assert "sqlalchemy" not in body
