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


# ---------------------------------------------------------------- усиление защиты


def test_login_rate_limited_by_ip(client, user):
    from app.config import get_settings

    limit = get_settings().login_rate_limit
    token = anon_csrf(client)
    for _ in range(limit):
        r = client.post(
            f"{API}/auth/login",
            json={"email": "nobody@example.com", "password": "x"},
            headers={"X-CSRF-Token": token},
        )
        assert r.status_code == 401
    r = client.post(
        f"{API}/auth/login",
        json={"email": user.email, "password": PASSWORD},  # даже верный пароль
        headers={"X-CSRF-Token": token},
    )
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "TOO_MANY_ATTEMPTS"


def test_register_rate_limited_by_ip(client):
    from app.config import get_settings

    token = anon_csrf(client)
    for i in range(get_settings().register_rate_limit):
        r = client.post(
            f"{API}/auth/register",
            json={"email": f"r{i}@example.com", "password": "Passw0rd!", "display_name": "R"},
            headers={"X-CSRF-Token": token},
        )
        assert r.status_code == 201
    r = client.post(
        f"{API}/auth/register",
        json={"email": "rx@example.com", "password": "Passw0rd!", "display_name": "R"},
        headers={"X-CSRF-Token": token},
    )
    assert r.status_code == 429


def test_body_size_limit(user_client):
    from app.config import get_settings

    big = "а" * (get_settings().max_body_bytes // 2 + 10)  # кириллица — 2 байта на символ
    r = user_client.post(f"{API}/import/tbank/parse", json={"text": big})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_idle_session_expires(db, user_client):
    from datetime import UTC, datetime, timedelta

    from app.models import UserSession

    assert user_client.get(f"{API}/auth/me").status_code == 200
    for s in db.query(UserSession).all():
        s.last_seen_at = datetime.now(UTC) - timedelta(days=2)
    db.flush()
    assert user_client.get(f"{API}/auth/me").status_code == 401
    assert db.query(UserSession).count() == 0  # просроченная сессия удалена


def test_secure_cookie_names_use_host_prefix():
    from app.config import Settings

    secure = Settings(cookie_secure=True)
    assert (secure.session_cookie, secure.csrf_cookie) == ("__Host-fa_session", "__Host-fa_csrf")
    plain = Settings(cookie_secure=False)
    assert (plain.session_cookie, plain.csrf_cookie) == ("fa_session", "fa_csrf")


def test_extra_security_headers(client):
    h = client.get("/health").headers
    assert "camera=()" in h["Permissions-Policy"]
    assert h["Cross-Origin-Opener-Policy"] == "same-origin"


def test_statement_parser_resists_pathological_input():
    import time

    from app.services.statement_tbank import parse

    hostile = ("01.10.2026 12:00 01.10.2026 12:00 -1.00 ₽ -1.00 ₽ " + "x " * 50) * 1500
    start = time.monotonic()
    parse(hostile[:200_000])
    assert time.monotonic() - start < 2  # было ~20 с до ограничения длины описания


# Маршруты, доступные без входа. Любой новый маршрут без проверки авторизации уронит тест.
PUBLIC = {
    ("GET", "/health"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("GET", "/register"),
    ("POST", "/register"),
    ("POST", "/logout"),
    ("GET", "/api/v1/auth/csrf"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/register"),
    ("POST", "/api/v1/auth/logout"),
}


def _concrete(path: str) -> str:
    import re
    import uuid

    return re.sub(
        r"\{[^}]+\}", lambda m: "2026" if "year" in m.group() else str(uuid.uuid4()), path
    )


def _all_routes():
    """(метод, путь) всех маршрутов, включая вложенные роутеры (FastAPI хранит их иерархией)."""
    from fastapi.routing import APIRoute

    from app.main import app

    def walk(routes, prefix=""):
        for r in routes:
            if isinstance(r, APIRoute):
                for method in r.methods - {"HEAD", "OPTIONS"}:
                    yield method, prefix + r.path
            elif hasattr(r, "original_router"):
                yield from walk(r.original_router.routes, prefix + (r.include_context.prefix or ""))

    return list(walk(app.routes))


def test_every_route_requires_authentication(client):
    token = anon_csrf(client)
    checked = 0
    for method, path in _all_routes():
        if (method, path) not in PUBLIC:
            r = client.request(
                method,
                _concrete(path),
                headers={"X-CSRF-Token": token},
                follow_redirects=False,
            )
            # API → 401; веб → редирект на страницу входа
            ok = r.status_code == 401 or (
                r.status_code == 303 and r.headers["location"].startswith("/login")
            )
            assert ok, f"{method} {path} доступен без входа: {r.status_code}"
            checked += 1
    assert checked > 50


def test_admin_routes_forbidden_for_regular_user(user_client):
    admin_routes = [(m, p) for m, p in _all_routes() if "/admin" in p]
    assert len(admin_routes) > 20
    for method, path in admin_routes:
        r = user_client.request(method, _concrete(path), follow_redirects=False)
        assert r.status_code == 403, f"{method} {path}: {r.status_code}"


def test_rate_limit_ignores_spoofed_forwarded_for(client):
    """Клиент подставляет новый X-Forwarded-For на каждую попытку — лимит всё равно работает:
    берётся адрес, дописанный прокси (справа), а не присланный клиентом (слева)."""
    from app.config import get_settings

    token = anon_csrf(client)
    statuses = [
        client.post(
            f"{API}/auth/login",
            json={"email": "x@example.com", "password": "x"},
            headers={"X-CSRF-Token": token, "X-Forwarded-For": f"10.0.0.{i}, 203.0.113.7"},
        ).status_code
        for i in range(get_settings().login_rate_limit + 1)
    ]
    assert statuses[-1] == 429


def test_client_ip_from_rightmost_proxy_hop():
    from starlette.requests import Request

    from app.ratelimit import client_ip

    def req(xff):
        headers = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "headers": headers, "client": ("172.16.0.1", 1)})

    assert client_ip(req("1.1.1.1, 2.2.2.2, 203.0.113.7")) == "203.0.113.7"
    assert client_ip(req(None)) == "172.16.0.1"
