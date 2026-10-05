"""Миграции на отдельной временной БД: upgrade/downgrade, соответствие моделям, seed-данные."""

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

from app.models import Base
from tests.conftest import TEST_DATABASE_URL, alembic_config

D = Decimal


@pytest.fixture(scope="module")
def migr_url() -> Iterator[str]:
    name = f"fin_acc_migr_{uuid.uuid4().hex[:12]}"
    server = create_engine(
        make_url(TEST_DATABASE_URL).set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    with server.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with server.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        server.dispose()


@pytest.fixture(scope="module")
def migr_engine(migr_url: str) -> Iterator[Engine]:
    eng = create_engine(migr_url)
    yield eng
    eng.dispose()


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def test_upgrade_downgrade_upgrade(migr_url, migr_engine):
    cfg = alembic_config(migr_url)
    command.upgrade(cfg, "head")
    assert _tables(migr_engine) == set(Base.metadata.tables)

    command.downgrade(cfg, "base")
    assert _tables(migr_engine) == set()
    with migr_engine.connect() as conn:
        enums = conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'")).scalars()
        assert set(enums) == set()

    command.upgrade(cfg, "head")
    assert _tables(migr_engine) == set(Base.metadata.tables)


def test_step_by_step_downgrade(migr_url, migr_engine):
    cfg = alembic_config(migr_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    with migr_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM account_types")).scalar() == 0
    command.upgrade(cfg, "head")


def test_models_match_migrations(migr_url, migr_engine):
    command.upgrade(alembic_config(migr_url), "head")
    with migr_engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == []


def test_seed_data(migr_url, migr_engine):
    command.upgrade(alembic_config(migr_url), "head")
    with migr_engine.connect() as conn:
        types = dict(conn.execute(text("SELECT code, single_per_user FROM account_types")).all())
        assert types == {
            "current": True,
            "monthly": False,
            "fund": False,
            "savings": True,
            "unallocated": True,
        }
        op_types = set(conn.execute(text("SELECT code FROM operation_types")).scalars())
        assert op_types == {"income", "expense", "transfer"}
        kinds = set(
            conn.execute(text("SELECT code FROM income_kinds WHERE code IS NOT NULL")).scalars()
        )
        assert kinds == {"salary", "bonus"}
        brackets = conn.execute(
            text("SELECT income_from, income_to, rate FROM tax_brackets ORDER BY income_from")
        ).all()
        assert [(D(f), None if t is None else D(t), D(r)) for f, t, r in brackets] == [
            (D("0"), D("2400000"), D("13")),
            (D("2400000"), D("5000000"), D("15")),
            (D("5000000"), D("20000000"), D("18")),
            (D("20000000"), D("50000000"), D("20")),
            (D("50000000"), None, D("22")),
        ]
        holidays_2026 = conn.execute(
            text("SELECT count(*) FROM holidays WHERE date_part('year', date_from) = 2026")
        ).scalar()
        assert holidays_2026 > 0
        assert (
            conn.execute(
                text("SELECT value FROM app_settings WHERE key = 'registration_open'")
            ).scalar()
            == "true"
        )


def test_downgrade_base_with_user_data(migr_url, migr_engine):
    """Откат до base не должен падать, если в БД уже есть пользователи, счета и операции."""
    from datetime import date

    from sqlalchemy.orm import Session

    from app import schemas
    from app.services import accounts as accounts_svc
    from app.services import operations as ops_svc
    from app.services import users as users_svc

    cfg = alembic_config(migr_url)
    command.upgrade(cfg, "head")
    with Session(migr_engine) as db:
        u = users_svc.create_user(db, "migr@example.com", "Passw0rd1", "M")
        acc = accounts_svc.get_unallocated_account(db, u)
        ops_svc.create_operation(
            db,
            u,
            schemas.OperationIn(
                operation_type="expense",
                account_id=acc.id,
                op_date=date(2026, 1, 1),
                name="Тест",
                amount=D("10"),
            ),
        )
    command.downgrade(cfg, "base")
    assert _tables(migr_engine) == set()
    command.upgrade(cfg, "head")
