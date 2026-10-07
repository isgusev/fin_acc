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


# ---------------------------------------------------------------- целевая сумма фонда


def fund_in(refs, **kw):
    data = {"name": "Отпуск", "account_type_id": refs.account_types["fund"]} | kw
    return schemas.AccountIn(**data)


@pytest.mark.parametrize(
    ("target", "months", "period", "expected"),
    [
        ("5000", 5, "monthly", "1000.00"),  # 5000 / (5 / 1)
        ("15000", 9, "quarterly", "5000.00"),  # 15000 / (9 / 3)
        ("120000", 24, "yearly", "60000.00"),  # 120000 / (24 / 12)
        ("5200", 12, "weekly", "100.00"),  # 52 недели за год
        ("1000", 3, "monthly", "333.33"),
    ],
)
def test_fund_replenish_amount_from_target(db, user, refs, target, months, period, expected):
    acc = accounts_svc.create_account(
        db,
        user,
        fund_in(
            refs,
            target_amount=D(target),
            months_to_goal=months,
            replenish_period=period,
            replenish_amount=D("1"),
        ),  # введённая вручную сумма игнорируется
    )
    assert acc.replenish_amount == D(expected)
    assert acc.target_amount == D(target)


def test_fund_target_recalculated_on_update(db, user, refs):
    acc = accounts_svc.create_account(
        db,
        user,
        fund_in(refs, target_amount=D("5000"), months_to_goal=5, replenish_period="monthly"),
    )
    acc = accounts_svc.update_account(
        db,
        user,
        acc.id,
        fund_in(refs, target_amount=D("6000"), months_to_goal=6, replenish_period="quarterly"),
    )
    assert acc.replenish_amount == D("3000.00")


@pytest.mark.parametrize(
    ("kw", "field"),
    [
        ({"months_to_goal": None, "replenish_period": "monthly"}, "months_to_goal"),
        ({"months_to_goal": 5, "replenish_period": "none"}, "replenish_period"),
        ({"months_to_goal": 2, "replenish_period": "quarterly"}, "months_to_goal"),
    ],
)
def test_fund_target_requires_term_and_period(db, user, refs, kw, field):
    with pytest.raises(ValidationAppError) as ei:
        accounts_svc.create_account(db, user, fund_in(refs, target_amount=D("5000"), **kw))
    assert ei.value.field == field


def test_target_amount_only_for_funds(db, user, refs):
    with pytest.raises(ValidationAppError) as ei:
        accounts_svc.create_account(
            db,
            user,
            schemas.AccountIn(
                name="Еда",
                account_type_id=refs.account_types["monthly"],
                target_amount=D("5000"),
                months_to_goal=5,
                replenish_period="monthly",
            ),
        )
    assert ei.value.field == "target_amount"


def test_reopen_fund_keeps_target(db, user, refs):
    acc = accounts_svc.create_account(
        db,
        user,
        fund_in(refs, target_amount=D("5000"), months_to_goal=5, replenish_period="monthly"),
    )
    _, new, _ = accounts_svc.reopen_fund(db, user, acc.id)
    assert (new.target_amount, new.replenish_amount) == (D("5000.00"), D("1000.00"))


# ---------------------------------------------------------------- сводка пополнений


def test_replenishment_summary_monthly(db, user, refs, make):
    src = make.account(user, "current")
    fund = accounts_svc.create_account(
        db, user, fund_in(refs, replenish_period="monthly", replenish_amount=D("1000"))
    )
    make.transfer(user, src, fund, "400", on=date(2026, 10, 2))
    make.transfer(user, src, fund, "200", on=date(2026, 10, 20))
    make.transfer(user, src, fund, "1500", on=date(2026, 9, 5))
    make.transfer(user, src, fund, "999", on=date(2026, 3, 1))  # старше 6 месяцев

    rows = accounts_svc.replenishment_summary(db, user, fund, today=date(2026, 10, 25))
    assert [r.label for r in rows] == [
        "Октябрь 2026",
        "Сентябрь 2026",
        "Август 2026",
        "Июль 2026",
        "Июнь 2026",
        "Май 2026",
    ]
    assert (rows[0].amount, rows[0].percent) == (D("600.00"), 60)
    assert (rows[1].amount, rows[1].percent) == (D("1500.00"), 150)  # перевыполнение
    assert (rows[2].amount, rows[2].percent) == (D("0.00"), 0)


def test_replenishment_summary_quarterly(db, user, refs, make):
    """В октябре 2026 последние 6 месяцев (май–октябрь) задевают II, III и IV кварталы."""
    src = make.account(user, "current")
    fund = accounts_svc.create_account(
        db, user, fund_in(refs, replenish_period="quarterly", replenish_amount=D("3000"))
    )
    make.transfer(user, src, fund, "600", on=date(2026, 4, 10))  # II кв. — целиком в строке
    make.transfer(user, src, fund, "1000", on=date(2026, 7, 10))
    make.transfer(user, src, fund, "500", on=date(2026, 8, 10))
    make.transfer(user, src, fund, "300", on=date(2026, 10, 1))
    rows = accounts_svc.replenishment_summary(db, user, fund, date(2026, 10, 5))
    assert [(r.label, r.amount, r.percent) for r in rows] == [
        ("IV кв. 2026", D("300.00"), 10),
        ("III кв. 2026", D("1500.00"), 50),
        ("II кв. 2026", D("600.00"), 20),
    ]


@pytest.mark.parametrize(
    ("today", "labels"),
    [
        (date(2026, 10, 5), ["2026 год"]),
        (date(2026, 3, 5), ["2026 год", "2025 год"]),  # окт. 2025 – март 2026
    ],
)
def test_replenishment_summary_yearly(db, user, refs, make, today, labels):
    fund = accounts_svc.create_account(
        db, user, fund_in(refs, replenish_period="yearly", replenish_amount=D("12000"))
    )
    make.transfer(user, make.account(user, "current"), fund, "3000", on=date(2026, 2, 1))
    rows = accounts_svc.replenishment_summary(db, user, fund, today)
    assert [r.label for r in rows] == labels
    assert (rows[0].amount, rows[0].percent) == (D("3000.00"), 25)


def test_replenishment_summary_weekly_is_monthly(db, user, refs, make):
    fund = accounts_svc.create_account(
        db, user, fund_in(refs, replenish_period="weekly", replenish_amount=D("700"))
    )
    rows = accounts_svc.replenishment_summary(db, user, fund, date(2026, 10, 5))
    assert len(rows) == 6 and rows[0].label == "Октябрь 2026"
    assert rows[0].target == D("3100.00")  # 700 × 31 / 7


def test_replenishment_summary_without_plan_and_reopen_excluded(db, user, refs, make):
    src = make.account(user, "current")
    fund = accounts_svc.create_account(db, user, fund_in(refs))  # регулярность не задана
    make.transfer(user, src, fund, "700", on=date.today())
    rows = accounts_svc.replenishment_summary(db, user, fund)
    assert len(rows) == 6
    assert rows[0].amount == D("700.00") and rows[0].percent is None

    # перенос остатка при «Открыть заново» пополнением не считается
    _, new, _ = accounts_svc.reopen_fund(db, user, fund.id)
    assert accounts_svc.replenishment_summary(db, user, new)[0].amount == D("0.00")


# ---------------------------------------------------------------- история плана пополнения


def monthly_in(refs, **kw):
    data = {
        "name": "Продукты",
        "account_type_id": refs.account_types["monthly"],
        "replenish_period": "monthly",
        "replenish_amount": D("20000"),
    } | kw
    return schemas.AccountIn(**data)


def test_plan_change_does_not_rewrite_past_periods(db, user, refs, make):
    """Пример из ТЗ: до 01.10.2026 — 20 000 из 20 000 (100 %), с октября план 25 000."""
    src = make.account(user, "current")
    acc = accounts_svc.create_account(db, user, monthly_in(refs))
    make.transfer(user, src, acc, "20000", on=date(2026, 9, 3))
    make.transfer(user, src, acc, "20000", on=date(2026, 10, 3))
    accounts_svc.update_account(
        db,
        user,
        acc.id,
        monthly_in(refs, replenish_amount=D("25000"), plan_effective_from=date(2026, 10, 15)),
    )
    rows = {
        r.label: r for r in accounts_svc.replenishment_summary(db, user, acc, date(2026, 10, 20))
    }
    sep, oct_ = rows["Сентябрь 2026"], rows["Октябрь 2026"]
    assert (sep.target, sep.percent, sep.plan_change) == (D("20000.00"), 100, None)
    assert (oct_.target, oct_.percent) == (D("25000.00"), 80)
    ch = oct_.plan_change
    assert ch is not None and ch.effective_from == date(2026, 10, 1)  # учитывается месяц
    assert (ch.old_amount, ch.new_amount) == (D("20000.00"), D("25000.00"))


def test_plan_effective_next_month_keeps_current(db, user, refs):
    acc = accounts_svc.create_account(db, user, monthly_in(refs))
    accounts_svc.update_account(
        db,
        user,
        acc.id,
        monthly_in(refs, replenish_amount=D("30000"), plan_effective_from=date(2026, 11, 1)),
    )
    rows = accounts_svc.replenishment_summary(db, user, acc, date(2026, 10, 20))
    assert rows[0].target == D("20000.00")  # октябрь — ещё старый план
    assert accounts_svc.replenishment_summary(db, user, acc, date(2026, 11, 5))[0].target == D(
        "30000.00"
    )


def test_editing_without_plan_change_adds_no_plan(db, user, refs):
    acc = accounts_svc.create_account(db, user, monthly_in(refs))
    accounts_svc.update_account(
        db, user, acc.id, monthly_in(refs, name="Еда", plan_effective_from=date(2026, 10, 1))
    )
    plans = accounts_svc.replenish_plans(db, acc.id)
    assert [(p.effective_from, p.amount) for p in plans] == [(date(2000, 1, 1), D("20000.00"))]


def test_plan_change_replaces_later_plans(db, user, refs):
    acc = accounts_svc.create_account(db, user, monthly_in(refs))
    for amount, eff in (("25000", date(2026, 10, 1)), ("30000", date(2026, 12, 1))):
        accounts_svc.update_account(
            db,
            user,
            acc.id,
            monthly_in(refs, replenish_amount=D(amount), plan_effective_from=eff),
        )
    # более ранняя дата заменяет планы, начинавшиеся с неё и позже
    accounts_svc.update_account(
        db,
        user,
        acc.id,
        monthly_in(refs, replenish_amount=D("22000"), plan_effective_from=date(2026, 9, 10)),
    )
    plans = accounts_svc.replenish_plans(db, acc.id)
    assert [(p.effective_from, p.amount) for p in plans] == [
        (date(2000, 1, 1), D("20000.00")),
        (date(2026, 9, 1), D("22000.00")),
    ]


def test_regularity_change_scales_old_plan_to_new_rows(db, user, refs):
    """Был ежемесячный план 20 000, с октября — ежеквартальный 50 000.
    Строка III кв. считается по старому плану: 20 000 × 3 = 60 000."""
    acc = accounts_svc.create_account(db, user, monthly_in(refs))
    accounts_svc.update_account(
        db,
        user,
        acc.id,
        monthly_in(
            refs,
            replenish_period="quarterly",
            replenish_amount=D("50000"),
            plan_effective_from=date(2026, 10, 1),
        ),
    )
    rows = {
        r.label: r for r in accounts_svc.replenishment_summary(db, user, acc, date(2026, 10, 20))
    }
    assert rows["III кв. 2026"].target == D("60000.00")
    assert rows["IV кв. 2026"].target == D("50000.00")
    assert rows["IV кв. 2026"].plan_change is not None


def test_new_accounts_and_reopen_get_initial_plan(db, user, refs):
    acc = accounts_svc.create_account(db, user, monthly_in(refs))
    assert len(accounts_svc.replenish_plans(db, acc.id)) == 1
    unalloc = accounts_svc.get_unallocated_account(db, user)
    assert len(accounts_svc.replenish_plans(db, unalloc.id)) == 1
    fund = accounts_svc.create_account(
        db, user, fund_in(refs, replenish_period="monthly", replenish_amount=D("1000"))
    )
    _, new, _ = accounts_svc.reopen_fund(db, user, fund.id)
    (plan,) = accounts_svc.replenish_plans(db, new.id)
    assert (plan.period, plan.amount) == ("monthly", D("1000.00"))


def test_plan_effective_from_web_form(db, user_client, user, refs):
    acc = accounts_svc.create_account(db, user, monthly_in(refs))
    page = user_client.get(f"/accounts/{acc.id}/edit")
    assert 'type="month"' in page.text and "действует с" in page.text
    assert 'name="plan_effective_from"' not in user_client.get("/accounts/new").text
    r = user_client.post(
        f"/accounts/{acc.id}/edit",
        data={
            "name": "Продукты",
            "account_type_id": str(refs.account_types["monthly"]),
            "replenish_period": "monthly",
            "replenish_amount": "25 000",
            "plan_effective_from": "2026-10",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    plans = accounts_svc.replenish_plans(db, acc.id)
    assert plans[-1].effective_from == date(2026, 10, 1)


# ---------------------------------------------------------------- сводка месяца


def _overview(db, user, today):
    accs = accounts_svc.list_accounts(db, user, type_code="fund", include_closed=False)
    items = accounts_svc.accounts_with_balances(db, user, accs)
    return accounts_svc.month_overview(db, user, items, today)


def test_month_overview_rows_and_totals(db, user, refs, make):
    src = make.account(user, "current")
    monthly_fund = accounts_svc.create_account(
        db,
        user,
        fund_in(refs, name="Ремонт", replenish_period="monthly", replenish_amount=D("10000")),
    )
    quarterly = accounts_svc.create_account(
        db,
        user,
        fund_in(refs, name="Отпуск", replenish_period="quarterly", replenish_amount=D("6000")),
    )
    no_plan = accounts_svc.create_account(db, user, fund_in(refs, name="Подушка"))
    make.transfer(user, src, monthly_fund, "5000", on=date(2026, 10, 3))
    make.transfer(user, src, monthly_fund, "7000", on=date(2026, 9, 3))  # прошлый месяц
    make.transfer(user, src, quarterly, "2000", on=date(2026, 10, 10))
    make.transfer(user, src, no_plan, "3000", on=date(2026, 10, 11))

    ov = _overview(db, user, date(2026, 10, 20))
    assert ov.month_label == "Октябрь 2026"
    rows = {r.name: r for r in ov.rows}
    assert (rows["Ремонт"].replenished, rows["Ремонт"].expected, rows["Ремонт"].percent) == (
        D("5000.00"),
        D("10000.00"),
        50,
    )
    # квартальный план приводится к месяцу: 6000 / 3 = 2000
    assert (rows["Отпуск"].expected, rows["Отпуск"].percent) == (D("2000.00"), 100)
    assert (rows["Подушка"].replenished, rows["Подушка"].expected) == (D("3000.00"), None)
    assert rows["Ремонт"].balance == D("12000.00")
    # итог — только по счетам с планом: (5000 + 2000) / (10000 + 2000)
    assert (ov.total_replenished, ov.total_expected, ov.total_percent) == (
        D("7000.00"),
        D("12000.00"),
        58,
    )
    assert ov.total_balance == D("17000.00")


def test_month_overview_uses_plan_of_current_month(db, user, refs):
    acc = accounts_svc.create_account(
        db, user, fund_in(refs, replenish_period="monthly", replenish_amount=D("1000"))
    )
    accounts_svc.update_account(
        db,
        user,
        acc.id,
        fund_in(
            refs,
            replenish_period="monthly",
            replenish_amount=D("4000"),
            plan_effective_from=date(2026, 11, 1),
        ),
    )
    assert _overview(db, user, date(2026, 10, 5)).rows[0].expected == D("1000.00")
    assert _overview(db, user, date(2026, 11, 5)).rows[0].expected == D("4000.00")


def test_group_page_shows_overview(user_client, db, user, refs, make):
    accounts_svc.create_account(
        db, user, fund_in(refs, replenish_period="monthly", replenish_amount=D("1000"))
    )
    html = user_client.get("/funds").text
    assert "Сводка ·" in html and "Пополнение в этом месяце" in html and "Итого" in html
