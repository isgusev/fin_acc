"""Общие фикстуры тестов.

Тесты работают только с отдельной БД (TEST_DATABASE_URL, по умолчанию fin_acc_test).
Переменные окружения выставляются ДО импорта приложения: app.db создаёт engine при импорте.
"""

import os
from collections.abc import Callable, Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.engine import make_url

DEFAULT_TEST_DB = "postgresql+psycopg://fin_acc:fin_acc@localhost:5434/fin_acc_test"
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DB)

_db_name = make_url(TEST_DATABASE_URL).database or ""
if _db_name == "fin_acc" or not _db_name.endswith("_test"):
    raise RuntimeError(f"Тесты нельзя запускать на БД «{_db_name}»: нужна отдельная *_test БД")

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["APP_ENV"] = "test"
os.environ["ADMIN_EMAIL"] = ""  # bootstrap_admin в lifespan ничего не создаёт
os.environ["ADMIN_PASSWORD"] = ""
os.environ["FORCE_HTTPS"] = "false"
os.environ["COOKIE_SECURE"] = "false"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import schemas  # noqa: E402
from app.db import engine as app_engine  # noqa: E402
from app.db import get_db  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    AccountType,
    ExpenseCategory,
    IncomeKind,
    Operation,
    Planning,
    User,
    UserRole,
)
from app.services import accounts as accounts_svc  # noqa: E402
from app.services import operations as ops_svc  # noqa: E402
from app.services import planning as plan_svc  # noqa: E402
from app.services import users as users_svc  # noqa: E402

assert str(app_engine.url.database) == _db_name, "app.db.engine должен смотреть на тестовую БД"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "Secret-pass1"
API = "/api/v1"


def alembic_config(url: str) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


@pytest.fixture(scope="session", autouse=True)
def _migrated_db() -> Iterator[None]:
    """Чистая схема тестовой БД + alembic upgrade head один раз на сессию."""
    eng = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    eng.dispose()
    command.upgrade(alembic_config(TEST_DATABASE_URL), "head")
    yield


@pytest.fixture
def db() -> Iterator[Session]:
    """Сессия внутри внешней транзакции; commit() сервисов лишь освобождает savepoint."""
    conn = app_engine.connect()
    trans = conn.begin()
    session = Session(
        bind=conn,
        join_transaction_mode="create_savepoint",
        autoflush=False,
        expire_on_commit=False,
    )
    try:
        yield session
    finally:
        session.close()
        if trans.is_active:
            trans.rollback()
        conn.close()


# ---------------------------------------------------------------- пользователи


@pytest.fixture
def user(db: Session) -> User:
    return users_svc.create_user(db, "alice@example.com", PASSWORD, "Алиса")


@pytest.fixture
def other_user(db: Session) -> User:
    return users_svc.create_user(db, "bob@example.com", PASSWORD, "Боб")


@pytest.fixture
def admin(db: Session) -> User:
    return users_svc.create_user(db, "admin@example.com", PASSWORD, "Админ", UserRole.ADMIN)


# ---------------------------------------------------------------- справочники


class Refs:
    def __init__(self, db: Session) -> None:
        self.account_types = {
            t.code: t.id for t in db.scalars(select(AccountType)) if t.code is not None
        }
        self.income_kinds = {k.name: k.id for k in db.scalars(select(IncomeKind))}
        self.salary_kind = self.income_kinds["Зарплата"]
        self.bonus_kind = self.income_kinds["Премия"]
        self.other_kind = self.income_kinds["Иное"]
        self.rent_kind = self.income_kinds["Аренда"]
        self.categories = {c.name: c.id for c in db.scalars(select(ExpenseCategory))}
        self.misc_category = self.categories["Разное"]


@pytest.fixture
def refs(db: Session) -> Refs:
    return Refs(db)


# ---------------------------------------------------------------- фабрики данных


class Factory:
    """Короткие помощники для создания данных через сервисный слой."""

    def __init__(self, db: Session, refs: Refs) -> None:
        self.db = db
        self.refs = refs

    def unallocated(self, user: User) -> Account:
        return accounts_svc.get_unallocated_account(self.db, user)

    def account(self, user: User, code: str = "monthly", name: str | None = None) -> Account:
        return accounts_svc.create_account(
            self.db,
            user,
            schemas.AccountIn(
                name=name or f"{code}-счёт", account_type_id=self.refs.account_types[code]
            ),
        )

    def income_plan(
        self,
        user: User,
        amount: str | Decimal = "100000",
        on: date = date(2026, 1, 10),
        kind_id: int | None = None,
        **kw: object,
    ) -> Planning:
        return plan_svc.create_plan(
            self.db,
            user,
            schemas.PlanningIn(
                operation_type="income",
                planned_date=on,
                amount_planned=Decimal(amount),
                income_kind_id=kind_id or self.refs.other_kind,
                **kw,
            ),
        )

    def expense_plan(
        self, user: User, amount: str = "1000", on: date = date(2026, 1, 20), **kw: object
    ) -> Planning:
        return plan_svc.create_plan(
            self.db,
            user,
            schemas.PlanningIn(
                operation_type="expense", planned_date=on, amount_planned=Decimal(amount), **kw
            ),
        )

    def income(
        self,
        user: User,
        amount: str | Decimal,
        plan: Planning | None = None,
        on: date = date(2026, 1, 10),
        **kw: object,
    ) -> Operation:
        plan = plan or self.income_plan(user, amount, on)
        return ops_svc.create_operation(
            self.db,
            user,
            schemas.OperationIn(
                operation_type="income",
                op_date=on,
                amount=Decimal(amount),
                plan_id=plan.id,
                **kw,
            ),
        )

    def expense(
        self,
        user: User,
        account: Account,
        amount: str | Decimal,
        on: date = date(2026, 1, 15),
        name: str = "Покупка",
        **kw: object,
    ) -> Operation:
        # для расхода со счёта «Текущий» категория обязательна — подставляем «Разное»
        if account.account_type.code == "current":
            kw.setdefault("category_id", self.refs.misc_category)
        return ops_svc.create_operation(
            self.db,
            user,
            schemas.OperationIn(
                operation_type="expense",
                account_id=account.id,
                op_date=on,
                amount=Decimal(amount),
                name=name,
                **kw,
            ),
        )

    def transfer(
        self,
        user: User,
        src: Account,
        dst: Account,
        amount: str | Decimal,
        on: date = date(2026, 1, 12),
        **kw: object,
    ) -> Operation:
        return ops_svc.create_operation(
            self.db,
            user,
            schemas.OperationIn(
                operation_type="transfer",
                account_id=src.id,
                target_account_id=dst.id,
                op_date=on,
                amount=Decimal(amount),
                **kw,
            ),
        )


@pytest.fixture
def make(db: Session, refs: Refs) -> Factory:
    return Factory(db, refs)


# ---------------------------------------------------------------- HTTP


@pytest.fixture
def app_(db: Session) -> Iterator[object]:
    from app.main import app

    def _get_db() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _get_db
    try:
        yield app
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def new_client(app_: object) -> Iterator[Callable[[], TestClient]]:
    """Фабрика независимых клиентов (у каждого свои cookie)."""
    clients: list[TestClient] = []

    def factory() -> TestClient:
        c = TestClient(app_)  # type: ignore[arg-type]
        c.__enter__()
        clients.append(c)
        return c

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(new_client: Callable[[], TestClient]) -> TestClient:
    return new_client()


def anon_csrf(client: TestClient) -> str:
    r = client.get(f"{API}/auth/csrf")
    assert r.status_code == 200
    return r.json()["csrf_token"]


def login(client: TestClient, email: str, password: str = PASSWORD) -> dict:
    """Вход; сохраняет CSRF-токен сессии в заголовках клиента. Возвращает JSON ответа."""
    client.headers.pop("X-CSRF-Token", None)
    token = anon_csrf(client)
    r = client.post(
        f"{API}/auth/login",
        json={"email": email, "password": password},
        headers={"X-CSRF-Token": token},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    client.headers["X-CSRF-Token"] = body["csrf_token"]
    return body


@pytest.fixture
def user_client(client: TestClient, user: User) -> TestClient:
    login(client, user.email)
    return client


@pytest.fixture
def other_client(new_client: Callable[[], TestClient], other_user: User) -> TestClient:
    c = new_client()
    login(c, other_user.email)
    return c


@pytest.fixture
def admin_client(new_client: Callable[[], TestClient], admin: User) -> TestClient:
    c = new_client()
    login(c, admin.email)
    return c


@pytest.fixture(autouse=True)
def _reset_rate_limits() -> Iterator[None]:
    """Лимиты попыток по IP — в памяти процесса; каждый тест начинает с чистого листа."""
    from app import ratelimit

    ratelimit.reset()
    yield
    ratelimit.reset()
