"""Служебные команды.

python -m app.cli create-admin admin@example.com
"""

import argparse
import getpass
import sys

from app.db import SessionLocal
from app.errors import AppError
from app.models import UserRole
from app.services import users as users_svc


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
    args = parser.parse_args()
    if args.command == "create-admin":
        return create_admin(args.email)
    return 2


if __name__ == "__main__":
    sys.exit(main())
