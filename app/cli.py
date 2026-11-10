"""Служебные команды.

python -m app.cli create-admin admin@example.com
python -m app.cli wait-db --timeout 60
"""

import argparse
import getpass
import sys
import time
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.db import SessionLocal, engine
from app.errors import AppError
from app.models import UserRole
from app.services import users as users_svc


def _ping() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def wait_db(
    timeout: float,
    interval: float = 2.0,
    ping: Callable[[], None] = _ping,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Ждёт, пока база начнёт принимать подключения (не дольше timeout секунд).

    На хостинге база и приложение могут перезапускаться одновременно: без ожидания
    приложение падает на миграциях, если база ещё поднимается.
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            ping()
            if attempt > 1:
                print(f"База данных доступна (попытка {attempt})", flush=True)
            return 0
        except OperationalError as e:
            reason = str(e.orig).strip().splitlines()[0] if e.orig else str(e)
            if time.monotonic() >= deadline:
                print(f"База данных недоступна {timeout:.0f} с: {reason}", file=sys.stderr)
                return 1
            print(f"Жду базу данных (попытка {attempt}): {reason}", flush=True)
            sleep(interval)


def create_admin(email: str) -> int:
    password = getpass.getpass("Пароль администратора: ")
    if password != getpass.getpass("Повторите пароль: "):
        print("Пароли не совпадают", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        try:
            existing = users_svc.get_by_email(db, email)
            if existing is not None:
                existing.role = UserRole.ADMIN
                db.commit()
                print(f"Пользователю {email} назначена роль администратора")
            else:
                users_svc.create_user(db, email, password, "Администратор", UserRole.ADMIN)
                print(f"Администратор {email} создан")
        except AppError as e:
            print(f"Ошибка: {e.message}", file=sys.stderr)
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-admin", help="создать администратора или повысить пользователя")
    p.add_argument("email")
    w = sub.add_parser("wait-db", help="дождаться доступности базы данных")
    w.add_argument("--timeout", type=float, default=60.0, help="сколько секунд ждать")
    args = parser.parse_args()
    if args.command == "create-admin":
        return create_admin(args.email)
    if args.command == "wait-db":
        return wait_db(args.timeout)
    return 2


if __name__ == "__main__":
    sys.exit(main())
