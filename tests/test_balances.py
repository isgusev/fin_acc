"""Балансы счетов считаются по операциям."""

from datetime import date
from decimal import Decimal

from app import schemas
from app.services import operations as ops_svc
from app.services.balances import account_balance, account_balances, balances_by_type

D = Decimal


def test_new_account_has_zero_balance(db, user, make):
    acc = make.account(user, "current")
    assert account_balance(db, user.id, acc.id) == D("0.00")


def test_balance_formula(db, user, make):
    unalloc = make.unallocated(user)
    current = make.account(user, "current")
    fund = make.account(user, "fund", "Отпуск")

    make.income(user, "100000")
    make.income(user, "5000.50")
    make.transfer(user, unalloc, current, "60000")
    make.transfer(user, current, fund, "10000")
    make.expense(user, current, "1234.56")
    make.expense(user, fund, "500")

    b = account_balances(db, user.id)
    # доходы − расходы − исходящие переводы + входящие
    assert b[unalloc.id] == D("100000") + D("5000.50") - D("60000")
    assert b[current.id] == D("60000") - D("10000") - D("1234.56")
    assert b[fund.id] == D("10000") - D("500")
    assert account_balance(db, user.id, current.id) == D("48765.44")


def test_balance_as_of_date(db, user, make):
    unalloc = make.unallocated(user)
    make.income(user, "1000", on=date(2026, 1, 10))
    make.income(user, "500", on=date(2026, 2, 10))
    assert account_balances(db, user.id, as_of=date(2026, 1, 31))[unalloc.id] == D("1000.00")
    assert account_balances(db, user.id)[unalloc.id] == D("1500.00")


def test_balances_by_type(db, user, make, refs):
    unalloc = make.unallocated(user)
    m1 = make.account(user, "monthly", "Продукты")
    m2 = make.account(user, "monthly", "Транспорт")
    make.income(user, "10000")
    make.transfer(user, unalloc, m1, "3000")
    make.transfer(user, unalloc, m2, "2000")
    make.expense(user, m2, "500")

    by_type = {t.code: b for t, b in balances_by_type(db, user.id)}
    assert set(by_type) == {"current", "monthly", "fund", "savings", "unallocated"}
    assert by_type["monthly"] == D("4500.00")
    assert by_type["unallocated"] == D("5000.00")
    assert by_type["current"] == D("0.00")
    assert by_type["fund"] == D("0.00")


def test_editing_past_operation_changes_balance(db, user, make):
    current = make.account(user, "current")
    unalloc = make.unallocated(user)
    make.income(user, "10000", on=date(2026, 1, 5))
    make.transfer(user, unalloc, current, "10000", on=date(2026, 1, 6))
    exp = make.expense(user, current, "2000", on=date(2026, 1, 7))
    make.expense(user, current, "1000", on=date(2026, 3, 1))
    assert account_balance(db, user.id, current.id) == D("7000.00")

    ops_svc.update_operation(
        db,
        user,
        exp.id,
        schemas.OperationIn(
            operation_type="expense",
            account_id=current.id,
            op_date=date(2026, 1, 7),
            name="Покупка",
            amount=D("500"),
        ),
    )
    assert account_balance(db, user.id, current.id) == D("8500.00")

    ops_svc.delete_operation(db, user, exp.id)
    assert account_balance(db, user.id, current.id) == D("9000.00")


def test_balance_may_go_negative(db, user, make):
    current = make.account(user, "current")
    make.expense(user, current, "750.25")
    assert account_balance(db, user.id, current.id) == D("-750.25")


def test_balances_isolated_between_users(db, user, other_user, make):
    a = make.account(user, "current", "Общий")
    b = make.account(other_user, "current", "Общий")
    make.income(user, "1000")
    make.transfer(user, make.unallocated(user), a, "1000")
    make.expense(other_user, b, "300")

    assert account_balances(db, user.id) == {
        make.unallocated(user).id: D("0.00"),
        a.id: D("1000.00"),
    }
    assert account_balances(db, other_user.id) == {b.id: D("-300.00")}
    # чужой счёт не попадает в баланс пользователя, даже если запросить его явно
    assert account_balance(db, user.id, b.id) == D("0.00")

    by_type_other = {t.code: v for t, v in balances_by_type(db, other_user.id)}
    assert by_type_other["unallocated"] == D("0.00")
    assert by_type_other["current"] == D("-300.00")
