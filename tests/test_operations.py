"""Бизнес-правила операций: доход, расход, перевод."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app import schemas
from app.errors import ConflictError, NotFoundError, ValidationAppError
from app.services import accounts as accounts_svc
from app.services import operations as ops_svc
from app.services.balances import account_balance

D = Decimal
DAY = date(2026, 2, 1)


def op_in(**kw):
    data = {"op_date": DAY, "amount": D("100")}
    data |= kw
    return schemas.OperationIn(**data)


# ---------------------------------------------------------------- схема


@pytest.mark.parametrize("op_type", ["income", "expense", "transfer"])
@pytest.mark.parametrize("amount", ["0", "-1", "-0.01", "0.00"])
def test_amount_must_be_positive(op_type, amount):
    with pytest.raises(ValidationError) as ei:
        op_in(operation_type=op_type, amount=amount)
    assert ei.value.errors()[0]["loc"] == ("amount",)


def test_amount_max_two_decimals():
    with pytest.raises(ValidationError):
        op_in(operation_type="expense", amount="1.001")


def test_unknown_operation_type_rejected():
    with pytest.raises(ValidationError):
        op_in(operation_type="refund")


# ---------------------------------------------------------------- доход


def test_income_goes_to_unallocated_account(db, user, make):
    plan = make.income_plan(user)
    op = ops_svc.create_operation(db, user, op_in(operation_type="income", plan_id=plan.id))
    assert op.account_id == make.unallocated(user).id
    assert op.target_account_id is None
    assert op.plan_id == plan.id


def test_income_with_explicit_unallocated_account_ok(db, user, make):
    plan = make.income_plan(user)
    unalloc = make.unallocated(user)
    op = ops_svc.create_operation(
        db, user, op_in(operation_type="income", plan_id=plan.id, account_id=unalloc.id)
    )
    assert op.account_id == unalloc.id


def test_income_to_other_account_rejected(db, user, make):
    plan = make.income_plan(user)
    current = make.account(user, "current")
    with pytest.raises(ValidationAppError) as ei:
        ops_svc.create_operation(
            db, user, op_in(operation_type="income", plan_id=plan.id, account_id=current.id)
        )
    assert ei.value.field == "account_id"


def test_income_with_target_rejected(db, user, make):
    plan = make.income_plan(user)
    current = make.account(user, "current")
    with pytest.raises(ValidationAppError) as ei:
        ops_svc.create_operation(
            db,
            user,
            op_in(operation_type="income", plan_id=plan.id, target_account_id=current.id),
        )
    assert ei.value.field == "target_account_id"


def test_income_requires_plan(db, user):
    with pytest.raises(ValidationAppError) as ei:
        ops_svc.create_operation(db, user, op_in(operation_type="income"))
    assert ei.value.field == "plan_id"


def test_income_plan_type_must_match(db, user, make):
    plan = make.expense_plan(user)
    with pytest.raises(ValidationAppError) as ei:
        ops_svc.create_operation(db, user, op_in(operation_type="income", plan_id=plan.id))
    assert ei.value.field == "plan_id"


def test_income_with_other_users_plan_not_found(db, user, other_user, make):
    foreign = make.income_plan(other_user)
    with pytest.raises(NotFoundError) as ei:
        ops_svc.create_operation(db, user, op_in(operation_type="income", plan_id=foreign.id))
    assert ei.value.field == "plan_id"


def test_income_without_open_unallocated_account(db, user, make):
    plan = make.income_plan(user)
    accounts_svc.close_account(db, user, make.unallocated(user).id)
    with pytest.raises(ConflictError):
        ops_svc.create_operation(db, user, op_in(operation_type="income", plan_id=plan.id))


# ---------------------------------------------------------------- расход


def test_expense_ok_and_fields_stored(db, user, make):
    current = make.account(user, "current")
    plan = make.expense_plan(user, funding_account_id=current.id)
    op = ops_svc.create_operation(
        db,
        user,
        op_in(
            operation_type="expense",
            account_id=current.id,
            name="Продукты",
            category="Еда",
            comment="  Чек №5; 'quoted' \"double\"  ",
            plan_id=plan.id,
        ),
    )
    db.expire_all()
    stored = ops_svc.get_operation(db, user, op.id)
    assert stored.name == "Продукты"
    assert stored.category == "Еда"
    assert stored.comment == "Чек №5; 'quoted' \"double\""
    assert stored.plan_id == plan.id
    assert stored.amount == D("100.00")


def test_expense_requires_name(db, user, make):
    current = make.account(user, "current")
    for name in (None, "", "   "):
        with pytest.raises(ValidationAppError) as ei:
            ops_svc.create_operation(
                db, user, op_in(operation_type="expense", account_id=current.id, name=name)
            )
        assert ei.value.field == "name"


def test_expense_requires_account(db, user):
    with pytest.raises(ValidationAppError) as ei:
        ops_svc.create_operation(db, user, op_in(operation_type="expense", name="x"))
    assert ei.value.field == "account_id"


def test_expense_plan_type_must_match(db, user, make):
    current = make.account(user, "current")
    plan = make.income_plan(user)
    with pytest.raises(ValidationAppError) as ei:
        ops_svc.create_operation(
            db,
            user,
            op_in(operation_type="expense", account_id=current.id, name="x", plan_id=plan.id),
        )
    assert ei.value.field == "plan_id"


def test_expense_with_target_rejected(db, user, make):
    a = make.account(user, "current")
    b = make.account(user, "fund")
    with pytest.raises(ValidationAppError) as ei:
        ops_svc.create_operation(
            db,
            user,
            op_in(operation_type="expense", account_id=a.id, target_account_id=b.id, name="x"),
        )
    assert ei.value.field == "target_account_id"


def test_expense_on_other_users_account_not_found(db, user, other_user, make):
    foreign = make.account(other_user, "current")
    with pytest.raises(NotFoundError) as ei:
        ops_svc.create_operation(
            db, user, op_in(operation_type="expense", account_id=foreign.id, name="x")
        )
    assert ei.value.field == "account_id"


def test_expense_with_other_users_plan_not_found(db, user, other_user, make):
    current = make.account(user, "current")
    foreign = make.expense_plan(other_user)
    with pytest.raises(NotFoundError):
        ops_svc.create_operation(
            db,
            user,
            op_in(operation_type="expense", account_id=current.id, name="x", plan_id=foreign.id),
        )


def test_nonexistent_account_not_found(db, user):
    with pytest.raises(NotFoundError):
        ops_svc.create_operation(
            db, user, op_in(operation_type="expense", account_id=uuid.uuid4(), name="x")
        )


def test_expense_on_closed_account_rejected(db, user, make):
    acc = make.account(user, "monthly")
    accounts_svc.close_account(db, user, acc.id)
    with pytest.raises(ConflictError) as ei:
        ops_svc.create_operation(
            db, user, op_in(operation_type="expense", account_id=acc.id, name="x")
        )
    assert ei.value.field == "account_id"


# ---------------------------------------------------------------- перевод


def test_transfer_ok(db, user, make):
    a = make.account(user, "current")
    b = make.account(user, "savings")
    op = make.transfer(user, a, b, "250")
    assert op.account_id == a.id
    assert op.target_account_id == b.id
    assert account_balance(db, user.id, a.id) == D("-250.00")
    assert account_balance(db, user.id, b.id) == D("250.00")


def test_transfer_requires_source(db, user, make):
    b = make.account(user, "savings")
    with pytest.raises(ValidationAppError) as ei:
        ops_svc.create_operation(db, user, op_in(operation_type="transfer", target_account_id=b.id))
    assert ei.value.field == "account_id"


def test_transfer_requires_target(db, user, make):
    a = make.account(user, "current")
    with pytest.raises(ValidationAppError) as ei:
        ops_svc.create_operation(db, user, op_in(operation_type="transfer", account_id=a.id))
    assert ei.value.field == "target_account_id"


def test_transfer_accounts_must_differ(db, user, make):
    a = make.account(user, "current")
    with pytest.raises(ValidationAppError) as ei:
        ops_svc.create_operation(
            db, user, op_in(operation_type="transfer", account_id=a.id, target_account_id=a.id)
        )
    assert ei.value.field == "target_account_id"


def test_transfer_cannot_link_plan(db, user, make):
    a = make.account(user, "current")
    b = make.account(user, "savings")
    plan = make.expense_plan(user)
    with pytest.raises(ValidationAppError) as ei:
        ops_svc.create_operation(
            db,
            user,
            op_in(
                operation_type="transfer",
                account_id=a.id,
                target_account_id=b.id,
                plan_id=plan.id,
            ),
        )
    assert ei.value.field == "plan_id"


def test_transfer_to_other_users_account_not_found(db, user, other_user, make):
    a = make.account(user, "current")
    foreign = make.account(other_user, "savings")
    with pytest.raises(NotFoundError) as ei:
        make.transfer(user, a, foreign, "1")
    assert ei.value.field == "target_account_id"
    with pytest.raises(NotFoundError) as ei:
        make.transfer(user, foreign, a, "1")
    assert ei.value.field == "account_id"


def test_transfer_to_closed_account_rejected(db, user, make):
    a = make.account(user, "current")
    b = make.account(user, "fund")
    accounts_svc.close_account(db, user, b.id)
    with pytest.raises(ConflictError) as ei:
        make.transfer(user, a, b, "1")
    assert ei.value.field == "target_account_id"
    with pytest.raises(ConflictError) as ei:
        make.transfer(user, b, a, "1")
    assert ei.value.field == "account_id"


# ---------------------------------------------------------------- изменение и удаление


def test_past_operation_can_be_edited(db, user, make):
    acc = make.account(user, "current")
    op = make.expense(user, acc, "100", on=date(2025, 12, 31))
    updated = ops_svc.update_operation(
        db,
        user,
        op.id,
        op_in(
            operation_type="expense",
            account_id=acc.id,
            name="Новогодний стол",
            amount=D("4321.99"),
            op_date=date(2025, 11, 15),
            comment="исправлено",
        ),
    )
    assert updated.op_date == date(2025, 11, 15)
    assert updated.amount == D("4321.99")
    assert updated.comment == "исправлено"
    assert account_balance(db, user.id, acc.id) == D("-4321.99")


def test_update_revalidates_rules(db, user, make):
    acc = make.account(user, "current")
    op = make.expense(user, acc, "100")
    with pytest.raises(ValidationAppError):
        ops_svc.update_operation(
            db, user, op.id, op_in(operation_type="expense", account_id=acc.id, name=None)
        )


def test_operation_on_closed_account_cannot_be_changed(db, user, make):
    a = make.account(user, "current")
    fund = make.account(user, "fund")
    t_in = make.transfer(user, a, fund, "100")
    make.transfer(user, fund, a, "100")
    accounts_svc.close_account(db, user, fund.id)
    with pytest.raises(ConflictError):
        ops_svc.update_operation(
            db,
            user,
            t_in.id,
            op_in(operation_type="transfer", account_id=a.id, target_account_id=fund.id),
        )
    with pytest.raises(ConflictError):
        ops_svc.delete_operation(db, user, t_in.id)


def test_other_users_operation_not_found(db, user, other_user, make):
    acc = make.account(other_user, "current")
    op = make.expense(other_user, acc, "10")
    with pytest.raises(NotFoundError):
        ops_svc.get_operation(db, user, op.id)
    with pytest.raises(NotFoundError):
        ops_svc.delete_operation(db, user, op.id)


def test_list_operations_filters(db, user, other_user, make, refs):
    unalloc = make.unallocated(user)
    cur = make.account(user, "current")
    make.income(user, "1000", on=date(2026, 1, 10))
    make.transfer(user, unalloc, cur, "600", on=date(2026, 1, 11))
    make.expense(user, cur, "100", on=date(2026, 2, 1))
    make.expense(other_user, make.account(other_user, "current"), "5")

    def ls(**kw):
        rows, total = ops_svc.list_operations(db, user, schemas.OperationFilter(**kw))
        assert len(rows) == total
        return rows

    assert len(ls()) == 3
    assert [o.op_date for o in ls()] == sorted([o.op_date for o in ls()], reverse=True)
    assert len(ls(operation_type="expense")) == 1
    assert len(ls(account_id=cur.id)) == 2  # входящий перевод + расход
    assert len(ls(date_from=date(2026, 1, 11), date_to=date(2026, 1, 31))) == 1
    assert len(ls(account_type_id=refs.account_types["unallocated"])) == 2
