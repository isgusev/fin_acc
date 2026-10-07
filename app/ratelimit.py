"""Ограничение частоты запросов по IP (вход, регистрация) — скользящее окно в памяти.

Счётчики живут в процессе: при нескольких воркерах Gunicorn у каждого свой счётчик,
т.е. фактический лимит — лимит × число воркеров. Для защиты от перебора паролей этого
достаточно (вместе с блокировкой учётной записи после неудачных попыток).
"""

import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from app.config import get_settings
from app.errors import RateLimitedError

_lock = threading.Lock()
_hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def client_ip(request: Request) -> str:
    """IP клиента для лимитов.

    Нельзя брать левый адрес X-Forwarded-For (так делает uvicorn при forwarded-allow-ips=*):
    его присылает сам клиент. Берём адрес, дописанный нашим ближайшим прокси, — N-й справа.
    """
    hops = get_settings().trusted_proxy_hops
    forwarded = request.headers.get("x-forwarded-for")
    if hops > 0 and forwarded:
        chain = [h.strip() for h in forwarded.split(",") if h.strip()]
        if chain:
            return chain[-hops] if len(chain) >= hops else chain[0]
    # без прокси — адрес TCP-соединения (scope["client"] до подмены заголовками недоступен)
    return request.client.host if request.client else "unknown"


def hit(scope: str, key: str, limit: int, window_seconds: float) -> None:
    """Учитывает попытку; при превышении лимита — RateLimitedError."""
    now = time.monotonic()
    with _lock:
        q = _hits[(scope, key)]
        while q and q[0] <= now - window_seconds:
            q.popleft()
        if len(q) >= limit:
            raise RateLimitedError("Слишком много попыток. Подождите несколько минут")
        q.append(now)
        if len(_hits) > 50_000:  # защита от роста памяти при атаке с множества адресов
            for k in [k for k, v in _hits.items() if not v or v[-1] <= now - window_seconds]:
                del _hits[k]


def reset() -> None:
    with _lock:
        _hits.clear()


def limit_login(request: Request) -> None:
    s = get_settings()
    hit("login", client_ip(request), s.login_rate_limit, s.login_rate_window_minutes * 60)


def limit_register(request: Request) -> None:
    s = get_settings()
    hit("register", client_ip(request), s.register_rate_limit, s.register_rate_window_minutes * 60)
