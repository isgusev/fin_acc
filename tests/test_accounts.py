"""Правила счетов: уникальность, удаление, закрытие, «Открыть заново», история пополнений."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import schemas
from app.errors import ConflictError, NotFoundError, ValidationAppError
from app.models import Account, Operation
from app.services import accounts as accounts_svc
from app.services.balances import account_balance

D = Decimal


def acc_in(refs, code, name, **kw):
    return schemas.AccountIn(name=name, account_type_id=refs.account_types[code], **kw)


# ---------------------------------------------------------------- создание


def test_user_gets_unallocated_account_on_creation(db, user):
    accs = accounts_svc.list_accounts(db, user)
    assert [(a.account_type.code, a.name) for a in accs] == [
        ("unallocated", "Нераспределённый доход")
    ]


@pytest.mark.parametrize("code", ["current", "savings", "unallocated"])
def test_single_per_user_types(db, user, refs, code):
    if code != "unallocated":
        accounts_svc.create_account(db, user, acc_in(refs, code, "Первый"))
    with pytest.raises(ConflictError) as ei:
        accounts_svc.create_account(db, user, acc_in(refs, code, "Второй"))
    assert ei.value.field == "account_type_id"


def test_single_per_user_independent_between_users(db, user, other_user, refs):
    accounts_svc.create_account(db, user, acc_in(refs, "current", "Карта"))
    acc = accounts_svc.create_account(db, other_user, acc_in(refs, "current", "Карта"))
    assert acc.owner_id == other_user.id


def test_single_per_user_allows_new_after_closing(db, user, refs):
    first = accounts_svc.create_account(db, user, acc_in(refs, "current", "Карта"))
    accounts_svc.close_account(db, user, first.id)
    second = accounts_svc.create_account(db, user, acc_in(refs, "current", "Карта"))
    assert second.id != first.id


def test_multiple_monthly_and_fund_accounts_allowed(db, user, refs):
    for name in ("Еда", "Транспорт"):
        accounts_svc.create_account(db, user, acc_in(refs, "monthly", name))
    for name in ("Отпуск", "Ремонт"):
        accounts_svc.create_account(db, user, acc_in(refs, "fund", name))
    assert len(accounts_svc.list_accounts(db, user)) == 5


def test_open_account_name_unique_per_user_case_insensitive(db, user, other_user, refs):
    accounts_svc.create_account(db, user, acc_in(refs, "monthly", "Еда"))
    with pytest.raises(ConflictError) as ei:
        accounts_svc.create_account(db, user, acc_in(refs, "fund", "ЕДА"))
    assert ei.value.field == "name"
    # у другого пользователя — можно
    accounts_svc.create_account(db, other_user, acc_in(refs, "monthly", "Еда"))
    # сессия жива после отката savepoint
    assert len(accounts_svc.list_accounts(db, user)) == 2


def test_closed_account_name_can_be_reused(db, user, refs):
    a = accounts_svc.create_account(db, user, acc_in(refs, "monthly", "Еда"))
    accounts_svc.close_account(db, user, a.id)
    accounts_svc.create_account(db, user, acc_in(refs, "monthly", "Еда"))


def test_rename_to_existing_name_conflict(db, user, refs):
    accounts_svc.create_account(db, user, acc_in(refs, "monthly", "Еда"))
    b = accounts_svc.create_account(db, user, acc_in(refs, "monthly", "Транспорт"))
    with pytest.raises(ConflictError) as ei:
        accounts_svc.update_account(db, user, b.id, acc_in(refs, "monthly", "еда"))
    assert ei.value.field == "name"


def test_unknown_account_type_not_found(db, user):
    with pytest.raises(NotFoundError):
        accounts_svc.create_account(db, user, schemas.AccountIn(name="x", account_type_id=999999))


def test_replenish_fields_stored(db, user, refs):
    a = accounts_svc.create_account(
        db,
        user,
        acc_in(
            refs,
            "fund",
            "Отпуск",
            replenish_period="monthly",
            replenish_amount=D("15000"),
            months_to_goal=12,
        ),
    )
    assert a.replenish_period.value == "monthly"
    assert a.replenish_amount == D("15000")
    assert a.months_to_goal == 12


# ---------------------------------------------------------------- удаление


def test_delete_empty_account(db, user, make):
    acc = make.account(user, "fund")
    accounts_svc.delete_account(db, user, acc.id)
    assert db.get(Account, acc.id) is None


def test_cannot_delete_account_with_operation_as_source(db, user, make):
    a = make.account(user, "current")
    b = make.account(user, "fund")
    make.transfer(user, a, b, "10")
    with pytest.raises(ConflictError):
        accounts_svc.delete_account(db, user, a.id)


def test_cannot_delete_account_with_operation_as_target(db, user, make):
    a = make.account(user, "current")
    b = make.account(user, "fund")
    make.transfer(user, a, b, "10")
    with pytest.raises(ConflictError):
        accounts_svc.delete_account(db, user, b.id)


def test_cannot_delete_account_with_expense(db, user, make):
    a = make.account(user, "monthly")
    make.expense(user, a, "10")
    with pytest.raises(ConflictError):
        accounts_svc.delete_account(db, user, a.id)


def test_cannot_delete_account_used_as_plan_funding(db, user, make):
    a = make.account(user, "monthly")
    make.expense_plan(user, funding_account_id=a.id)
    with pytest.raises(ConflictError):
        accounts_svc.delete_account(db, user, a.id)


def test_cannot_delete_other_users_account(db, user, other_user, make):
    foreign = make.account(other_user, "fund")
    with pytest.raises(NotFoundError):
        accounts_svc.delete_account(db, user, foreign.id)
    assert db.get(Account, foreign.id) is not None


# ---------------------------------------------------------------- закрытие


def test_close_only_with_zero_balance(db, user, make):
    a = make.account(user, "current")
    b = make.account(user, "fund")
    make.transfer(user, a, b, "100")
    with pytest.raises(ConflictError) as exc:
        accounts_svc.close_account(db, user, b.id)
    assert "100,00 ₽" in exc.value.message
    with pytest.raises(ConflictError):  # отрицательный остаток тоже нельзя
        accounts_svc.close_account(db, user, a.id)
    make.transfer(user, b, a, "100")
    closed = accounts_svc.close_account(db, user, b.id)
    assert closed.is_closed
    assert closed.closed_at is not None


def test_close_twice_conflict(db, user, make):
    a = make.account(user, "fund")
    accounts_svc.close_account(db, user, a.id)
    with pytest.raises(ConflictError):
        accounts_svc.close_account(db, user, a.id)


def test_list_accounts_without_closed(db, user, make):
    a = make.account(user, "fund", "Старый")
    make.account(user, "fund", "Новый")
    accounts_svc.close_account(db, user, a.id)
    names = {x.name for x in accounts_svc.list_accounts(db, user, include_closed=False)}
    assert names == {"Новый", "Нераспределённый доход"}
    assert {x.name for x in accounts_svc.list_accounts(db, user, type_code="fund")} == {
        "Старый",
        "Новый",
    }


# ---------------------------------------------------------------- «Открыть заново»


def _ops_between(db, a, b):
    return db.scalars(
        select(Operation).where(Operation.account_id == a.id, Operation.target_account_id == b.id)
    ).all()


def test_reopen_fund_positive_balance(db, user, make):
    cur = make.account(user, "current")
    fund = make.account(user, "fund", "Отпуск")
    make.transfer(user, cur, fund, "5000")
    make.expense(user, fund, "1200")

    old, new, moved = accounts_svc.reopen_fund(db, user, fund.id, on_date=date(2026, 6, 1))
    assert moved == D("3800.00")
    assert old.id == fund.id
    assert old.is_closed
    assert not new.is_closed
    assert new.name == "Отпуск"
    assert new.account_type.code == "fund"
    assert account_balance(db, user.id, new.id) == D("3800.00")
    assert account_balance(db, user.id, old.id) == D("0.00")
    [op] = _ops_between(db, old, new)
    assert op.operation_type.code == "transfer"
    assert op.amount == D("3800.00")
    assert op.op_date == date(2026, 6, 1)


def test_reopen_fund_negative_balance(db, user, make):
    fund = make.account(user, "fund", "Ремонт")
    make.expense(user, fund, "3000")
    old, new, moved = accounts_svc.reopen_fund(db, user, fund.id, on_date=date(2026, 6, 1))
    assert moved == D("-3000.00")
    assert account_balance(db, user.id, new.id) == D("-3000.00")
    assert account_balance(db, user.id, old.id) == D("0.00")
    [op] = _ops_between(db, new, old)
    assert op.amount == D("3000.00")
    assert old.is_closed


def test_reopen_fund_zero_balance_no_transfer(db, user, make):
    fund = make.account(user, "fund", "Пусто")
    old, new, moved = accounts_svc.reopen_fund(db, user, fund.id)
    assert moved == D("0.00")
    assert not _ops_between(db, old, new) and not _ops_between(db, new, old)
    assert old.is_closed and not new.is_closed


def test_reopen_keeps_replenish_settings(db, user, refs):
    fund = accounts_svc.create_account(
        db,
        user,
        acc_in(
            refs,
            "fund",
            "Цель",
            replenish_period="quarterly",
            replenish_amount=D("1000"),
            months_to_goal=6,
        ),
    )
    _, new, _ = accounts_svc.reopen_fund(db, user, fund.id)
    assert (new.replenish_period.value, new.replenish_amount, new.months_to_goal) == (
        "quarterly",
        D("1000.00"),
        6,
    )


@pytest.mark.parametrize("code", ["current", "monthly", "savings"])
def test_reopen_non_fund_rejected(db, user, make, code):
    acc = make.account(user, code)
    with pytest.raises(ValidationAppError):
        accounts_svc.reopen_fund(db, user, acc.id)


def test_reopen_closed_fund_rejected(db, user, make):
    fund = make.account(user, "fund")
    accounts_svc.close_account(db, user, fund.id)
    with pytest.raises(ConflictError):
        accounts_svc.reopen_fund(db, user, fund.id)


def test_reopen_other_users_fund_not_found(db, user, other_user, make):
    fund = make.account(other_user, "fund")
    with pytest.raises(NotFoundError):
        accounts_svc.reopen_fund(db, user, fund.id)


# ---------------------------------------------------------------- смена типа


def test_type_change_blocked_if_operations_exist(db, user, make, refs):
    a = make.account(user, "monthly", "Еда")
    make.expense(user, a, "10")
    with pytest.raises(ConflictError) as ei:
        accounts_svc.update_account(db, user, a.id, acc_in(refs, "fund", "Еда"))
    assert ei.value.field == "account_type_id"
    # переименование без смены типа — можно
    renamed = accounts_svc.update_account(db, user, a.id, acc_in(refs, "monthly", "Продукты"))
    assert renamed.name == "Продукты"


def test_type_change_allowed_without_operations(db, user, make, refs):
    a = make.account(user, "monthly", "Еда")
    updated = accounts_svc.update_account(db, user, a.id, acc_in(refs, "fund", "Еда"))
    assert updated.account_type.code == "fund"


def test_type_change_respects_single_per_user(db, user, make, refs):
    make.account(user, "current", "Карта")
    a = make.account(user, "monthly", "Еда")
    with pytest.raises(ConflictError):
        accounts_svc.update_account(db, user, a.id, acc_in(refs, "current", "Еда"))


# ---------------------------------------------------------------- история пополнений


def test_replenishment_history(db, user, make):
    unalloc = make.unallocated(user)
    cur = make.account(user, "current")
    fund = make.account(user, "fund")
    inc = make.income(user, "1000", on=date(2026, 1, 10))
    t1 = make.transfer(user, unalloc, fund, "300", on=date(2026, 1, 11))
    t2 = make.transfer(user, cur, fund, "50", on=date(2026, 1, 20))
    t_out = make.transfer(user, fund, cur, "20", on=date(2026, 1, 21))
    make.expense(user, fund, "10")

    fund_hist = accounts_svc.replenishment_history(db, user, fund.id)
    assert [o.id for o in fund_hist] == [t2.id, t1.id]  # новые сверху, без исходящих/расходов
    assert [o.id for o in accounts_svc.replenishment_history(db, user, unalloc.id)] == [inc.id]
    assert [o.id for o in accounts_svc.replenishment_history(db, user, cur.id)] == [t_out.id]
