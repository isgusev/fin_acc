"""Планирование: налоги, пересчёт по прогрессивной шкале, расчёт плановой зарплаты."""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app import schemas
from app.errors import ConflictError, NotFoundError, ValidationAppError
from app.services import planning as plan_svc
from app.services import users as users_svc

D = Decimal


def plan_in(**kw):
    data = {"planned_date": date(2026, 3, 1), "amount_planned": D("1000")}
    data |= kw
    return schemas.PlanningIn(**data)


def salary_plans(db, user, year=2026):
    return [
        p
        for p in plan_svc.list_plans(db, user, year=year, operation_type="income")
        if p.income_kind and p.income_kind.code == "salary"
    ]


def set_profile(db, user, **kw):
    data = {"display_name": user.display_name} | kw
    users_svc.update_profile(db, user, schemas.ProfileIn(**data))


# ---------------------------------------------------------------- валидация


def test_income_requires_income_kind(db, user):
    with pytest.raises(ValidationAppError) as ei:
        plan_svc.create_plan(db, user, plan_in(operation_type="income"))
    assert ei.value.field == "income_kind_id"


def test_income_cannot_have_funding_account(db, user, make, refs):
    acc = make.account(user, "current")
    with pytest.raises(ValidationAppError) as ei:
        plan_svc.create_plan(
            db,
            user,
            plan_in(
                operation_type="income", income_kind_id=refs.other_kind, funding_account_id=acc.id
            ),
        )
    assert ei.value.field == "funding_account_id"


def test_unknown_income_kind_not_found(db, user):
    with pytest.raises(NotFoundError):
        plan_svc.create_plan(db, user, plan_in(operation_type="income", income_kind_id=999999))


def test_expense_funding_account(db, user, make):
    acc = make.account(user, "monthly", "Еда")
    p = plan_svc.create_plan(
        db, user, plan_in(operation_type="expense", funding_account_id=acc.id, name="Продукты")
    )
    assert p.funding_account_id == acc.id
    assert p.is_taxable is False and p.tax_rate is None and p.amount_net is None
    assert plan_svc.to_out(p).funding_account_name == "Еда"


def test_expense_funding_account_optional(db, user):
    p = plan_svc.create_plan(db, user, plan_in(operation_type="expense"))
    assert p.funding_account_id is None


def test_expense_with_other_users_funding_account_not_found(db, user, other_user, make):
    foreign = make.account(other_user, "monthly")
    with pytest.raises(NotFoundError) as ei:
        plan_svc.create_plan(
            db, user, plan_in(operation_type="expense", funding_account_id=foreign.id)
        )
    assert ei.value.field == "funding_account_id"


def test_expense_rejects_income_fields(db, user, refs):
    with pytest.raises(ValidationAppError) as ei:
        plan_svc.create_plan(
            db, user, plan_in(operation_type="expense", income_kind_id=refs.other_kind)
        )
    assert ei.value.field == "income_kind_id"
    with pytest.raises(ValidationAppError) as ei:
        plan_svc.create_plan(db, user, plan_in(operation_type="expense", is_taxable=True))
    assert ei.value.field == "is_taxable"


def test_planning_amount_must_be_positive():
    with pytest.raises(ValueError):
        plan_in(operation_type="expense", amount_planned=D("0"))


# ---------------------------------------------------------------- налоги


def test_salary_is_always_taxable_with_auto_rate(db, user, refs):
    p = plan_svc.create_plan(
        db,
        user,
        plan_in(
            operation_type="income",
            income_kind_id=refs.salary_kind,
            amount_planned=D("100000"),
            is_taxable=False,
            tax_rate=D("1"),  # игнорируется — ставка считается по шкале
        ),
    )
    assert p.is_taxable is True
    assert p.tax_rate == D("13")
    assert p.amount_net == D("87000.00")


def test_non_salary_taxable_requires_manual_rate(db, user, refs):
    with pytest.raises(ValidationAppError) as ei:
        plan_svc.create_plan(
            db,
            user,
            plan_in(operation_type="income", income_kind_id=refs.rent_kind, is_taxable=True),
        )
    assert ei.value.field == "tax_rate"


def test_non_salary_taxable_net_computed(db, user, refs):
    p = plan_svc.create_plan(
        db,
        user,
        plan_in(
            operation_type="income",
            income_kind_id=refs.rent_kind,
            is_taxable=True,
            tax_rate=D("13"),
            amount_planned=D("33333.33"),
        ),
    )
    assert p.tax_rate == D("13")
    assert p.amount_net == D("29000.00")  # 33333.33 * 0.87 = 28999.9971 → 29000.00


def test_non_taxable_income_net_equals_amount(db, user, refs):
    p = plan_svc.create_plan(
        db, user, plan_in(operation_type="income", income_kind_id=refs.other_kind, tax_rate=D("5"))
    )
    assert p.is_taxable is False
    assert p.tax_rate is None
    assert p.amount_net == D("1000")


def test_non_taxable_bonus(db, user, refs):
    p = plan_svc.create_plan(
        db, user, plan_in(operation_type="income", income_kind_id=refs.bonus_kind)
    )
    assert p.is_taxable is False
    assert p.tax_rate is None
    assert p.amount_net == D("1000")


def _income(db, user, kind, amount, on, taxable=True):
    return plan_svc.create_plan(
        db,
        user,
        plan_in(
            operation_type="income",
            income_kind_id=kind,
            amount_planned=D(amount),
            planned_date=on,
            is_taxable=taxable,
        ),
    )


def test_progressive_recompute_ordered_by_date(db, user, refs):
    # создаём в «неправильном» порядке: пересчёт идёт по дате
    may = _income(db, user, refs.salary_kind, "500000", date(2026, 5, 15))
    bonus = _income(db, user, refs.bonus_kind, "1000000", date(2026, 3, 15))
    jan = _income(db, user, refs.salary_kind, "2000000", date(2026, 1, 15))
    # другой год и другой пользователь не влияют
    next_year = _income(db, user, refs.salary_kind, "100000", date(2027, 1, 15))

    assert (jan.tax_rate, jan.amount_net) == (D("13"), D("1740000.00"))
    # база 2 000 000: 400 000 по 13% + 600 000 по 15%
    assert (bonus.tax_rate, bonus.amount_net) == (D("15"), D("858000.00"))
    # база 3 000 000 (зарплата + премия): всё по 15%
    assert (may.tax_rate, may.amount_net) == (D("15"), D("425000.00"))
    assert (next_year.tax_rate, next_year.amount_net) == (D("13"), D("87000.00"))


def test_non_taxable_bonus_still_counts_in_base(db, user, refs):
    _income(db, user, refs.bonus_kind, "2400000", date(2026, 1, 10), taxable=False)
    sal = _income(db, user, refs.salary_kind, "100000", date(2026, 2, 10))
    assert sal.tax_rate == D("15")
    assert sal.amount_net == D("85000.00")


def test_other_income_kinds_not_in_base(db, user, refs):
    plan_svc.create_plan(
        db,
        user,
        plan_in(
            operation_type="income",
            income_kind_id=refs.rent_kind,
            amount_planned=D("5000000"),
            planned_date=date(2026, 1, 1),
            is_taxable=True,
            tax_rate=D("13"),
        ),
    )
    sal = _income(db, user, refs.salary_kind, "100000", date(2026, 2, 10))
    assert sal.tax_rate == D("13")


def test_bases_isolated_between_users(db, user, other_user, refs):
    _income(db, other_user, refs.salary_kind, "3000000", date(2026, 1, 10))
    sal = _income(db, user, refs.salary_kind, "100000", date(2026, 2, 10))
    assert sal.tax_rate == D("13")


def test_delete_plan_triggers_recompute(db, user, refs):
    jan = _income(db, user, refs.salary_kind, "2000000", date(2026, 1, 15))
    bonus = _income(db, user, refs.bonus_kind, "1000000", date(2026, 3, 15))
    assert bonus.tax_rate == D("15")
    plan_svc.delete_plan(db, user, jan.id)
    assert bonus.tax_rate == D("13")
    assert bonus.amount_net == D("870000.00")


def test_update_plan_triggers_recompute(db, user, refs):
    jan = _income(db, user, refs.salary_kind, "2000000", date(2026, 1, 15))
    bonus = _income(db, user, refs.bonus_kind, "1000000", date(2026, 3, 15))
    # переносим зарплату после премии → премия первая в году
    plan_svc.update_plan(
        db,
        user,
        jan.id,
        plan_in(
            operation_type="income",
            income_kind_id=refs.salary_kind,
            amount_planned=D("2000000"),
            planned_date=date(2026, 6, 15),
        ),
    )
    assert bonus.tax_rate == D("13")
    assert bonus.amount_net == D("870000.00")
    # база 1 000 000: 1 400 000 по 13% + 600 000 по 15%
    assert jan.tax_rate == D("15")
    assert jan.amount_net == D("2000000") - D("182000") - D("90000")


def test_update_plan_moving_to_other_kind_recomputes_old_year(db, user, refs):
    jan = _income(db, user, refs.salary_kind, "2400000", date(2026, 1, 15))
    feb = _income(db, user, refs.salary_kind, "100000", date(2026, 2, 15))
    assert feb.tax_rate == D("15")
    plan_svc.update_plan(
        db,
        user,
        jan.id,
        plan_in(
            operation_type="income",
            income_kind_id=refs.other_kind,
            amount_planned=D("2400000"),
            planned_date=date(2026, 1, 15),
        ),
    )
    assert feb.tax_rate == D("13")
    assert jan.tax_rate is None


def test_cannot_delete_plan_linked_to_operations(db, user, make):
    plan = make.income_plan(user, "1000")
    make.income(user, "1000", plan=plan)
    with pytest.raises(ConflictError):
        plan_svc.delete_plan(db, user, plan.id)


def test_cannot_change_type_of_plan_with_linked_operations(db, user, make):
    plan = make.income_plan(user, "1000")
    make.income(user, "1000", plan=plan)
    with pytest.raises(ConflictError):
        plan_svc.update_plan(db, user, plan.id, plan_in(operation_type="expense"))


def test_delete_unlinked_plan(db, user, make):
    plan = make.expense_plan(user)
    plan_svc.delete_plan(db, user, plan.id)
    with pytest.raises(NotFoundError):
        plan_svc.get_plan(db, user, plan.id)


def test_other_users_plan_not_found(db, user, other_user, make):
    plan = make.expense_plan(other_user)
    with pytest.raises(NotFoundError):
        plan_svc.get_plan(db, user, plan.id)
    with pytest.raises(NotFoundError):
        plan_svc.delete_plan(db, user, plan.id)
    with pytest.raises(NotFoundError):
        plan_svc.update_plan(db, user, plan.id, plan_in(operation_type="expense"))
    with pytest.raises(NotFoundError):
        plan_svc.get_plan(db, user, uuid.uuid4())


def test_amount_fact_sums_linked_operations(db, user, make):
    plan = make.income_plan(user, "1000")
    o1 = make.income(user, "400", plan=plan)
    o2 = make.income(user, "250.50", plan=plan)
    [out] = plan_svc.plans_out(db, [plan])
    assert out.amount_fact == D("650.50")
    assert set(out.operation_ids) == {o1.id, o2.id}


def test_year_totals(db, user, refs):
    _income(db, user, refs.salary_kind, "100000", date(2026, 1, 10))
    plan_svc.create_plan(
        db,
        user,
        plan_in(operation_type="expense", amount_planned=D("300"), planned_date=date(2026, 2, 1)),
    )
    plan_svc.create_plan(
        db,
        user,
        plan_in(operation_type="expense", amount_planned=D("9"), planned_date=date(2025, 2, 1)),
    )
    t = plan_svc.year_totals(db, user, 2026)
    assert t == {"income": D("100000"), "income_net": D("87000"), "expense": D("300")}


# ---------------------------------------------------------------- расчёт зарплаты


def test_salary_calc_requires_salary_day(db, user):
    set_profile(db, user, salary=D("100000"))
    with pytest.raises(ValidationAppError) as ei:
        plan_svc.calculate_salary(db, user, date(2026, 1, 1), False)
    assert ei.value.field == "salary_day"


@pytest.mark.parametrize("salary", [None, D("0")])
def test_salary_calc_requires_salary(db, user, salary):
    set_profile(db, user, salary=salary, salary_day=10)
    with pytest.raises(ValidationAppError) as ei:
        plan_svc.calculate_salary(db, user, date(2026, 1, 1), False)
    assert ei.value.field == "salary"


def test_salary_calc_advance_requires_calc_day(db, user):
    set_profile(db, user, salary=D("100000"), salary_day=10, advance_day=25)
    with pytest.raises(ValidationAppError) as ei:
        plan_svc.calculate_salary(db, user, date(2026, 1, 1), False)
    assert ei.value.field == "advance_calc_day"


def test_salary_calc_without_advance(db, user):
    set_profile(db, user, salary=D("100000"), salary_day=10)
    r = plan_svc.calculate_salary(db, user, date(2026, 3, 1), False)
    assert (len(r.created), r.replaced, r.skipped) == (10, 0, 0)
    plans = salary_plans(db, user)
    assert [p.planned_date for p in plans] == [date(2026, m, 10) for m in range(3, 13)]
    assert all(p.amount_planned == D("100000.00") for p in plans)
    assert all(p.is_auto and p.is_taxable and p.operation_type.code == "income" for p in plans)


def test_salary_calc_skips_dates_before_start(db, user):
    set_profile(db, user, salary=D("100000"), salary_day=10)
    r = plan_svc.calculate_salary(db, user, date(2026, 3, 15), False)
    assert len(r.created) == 9
    assert salary_plans(db, user)[0].planned_date == date(2026, 4, 10)


def test_salary_calc_start_on_salary_day_included(db, user):
    set_profile(db, user, salary=D("100000"), salary_day=10)
    plan_svc.calculate_salary(db, user, date(2026, 12, 10), False)
    assert [p.planned_date for p in salary_plans(db, user)] == [date(2026, 12, 10)]


def test_salary_calc_with_advance(db, user):
    set_profile(db, user, salary=D("300000"), salary_day=5, advance_day=20, advance_calc_day=15)
    r = plan_svc.calculate_salary(db, user, date(2026, 1, 1), False)
    assert len(r.created) == 24
    by_date = {p.planned_date: p.amount_planned for p in salary_plans(db, user)}
    assert len(by_date) == 24
    # январь: 15 рабочих дней (1–11 праздники), по 15-е — 4
    assert by_date[date(2026, 1, 20)] == D("80000.00")
    assert by_date[date(2026, 1, 5)] == D("220000.00")
    # февраль: 19 рабочих дней (23.02 праздник), по 15-е — 10
    assert by_date[date(2026, 2, 20)] == D("157894.74")
    assert by_date[date(2026, 2, 5)] == D("142105.26")
    for m in range(1, 13):
        assert by_date[date(2026, m, 20)] + by_date[date(2026, m, 5)] == D("300000.00")


def test_salary_calc_clamps_day_31(db, user):
    set_profile(db, user, salary=D("100000"), salary_day=31)
    plan_svc.calculate_salary(db, user, date(2026, 1, 1), False)
    dates = [p.planned_date for p in salary_plans(db, user)]
    assert len(dates) == 12
    assert date(2026, 1, 31) in dates
    assert date(2026, 2, 28) in dates
    assert date(2026, 4, 30) in dates
    assert date(2026, 11, 30) in dates


def test_salary_calc_replace_false_skips_existing(db, user):
    set_profile(db, user, salary=D("100000"), salary_day=10)
    plan_svc.calculate_salary(db, user, date(2026, 1, 1), False)
    set_profile(db, user, salary=D("200000"), salary_day=10)
    r = plan_svc.calculate_salary(db, user, date(2026, 1, 1), False)
    assert (len(r.created), r.replaced, r.skipped) == (0, 0, 12)
    plans = salary_plans(db, user)
    assert len(plans) == 12
    assert all(p.amount_planned == D("100000.00") for p in plans)


def test_salary_calc_replace_false_fills_new_dates_only(db, user):
    set_profile(db, user, salary=D("100000"), salary_day=10)
    plan_svc.calculate_salary(db, user, date(2026, 7, 1), False)
    r = plan_svc.calculate_salary(db, user, date(2026, 1, 1), False)
    assert (len(r.created), r.skipped) == (6, 6)
    assert len(salary_plans(db, user)) == 12


def test_salary_calc_replace_true_keeps_linked(db, user, make):
    set_profile(db, user, salary=D("100000"), salary_day=10)
    plan_svc.calculate_salary(db, user, date(2026, 1, 1), False)
    march = next(p for p in salary_plans(db, user) if p.planned_date == date(2026, 3, 10))
    make.income(user, "87000", plan=march, on=date(2026, 3, 10))
    # план до даты начала не трогается
    january = next(p for p in salary_plans(db, user) if p.planned_date == date(2026, 1, 10))

    set_profile(db, user, salary=D("150000"), salary_day=10)
    r = plan_svc.calculate_salary(db, user, date(2026, 2, 1), True)
    assert (len(r.created), r.replaced, r.skipped) == (10, 10, 1)

    plans = salary_plans(db, user)
    assert len(plans) == 12
    by_date = {p.planned_date: p for p in plans}
    assert by_date[date(2026, 3, 10)].id == march.id
    assert by_date[date(2026, 3, 10)].amount_planned == D("100000.00")
    assert by_date[date(2026, 1, 10)].id == january.id
    assert by_date[date(2026, 1, 10)].amount_planned == D("100000.00")
    assert by_date[date(2026, 4, 10)].amount_planned == D("150000.00")


def test_salary_calc_tax_rate_switches_after_threshold(db, user):
    set_profile(db, user, salary=D("500000"), salary_day=10)
    plan_svc.calculate_salary(db, user, date(2026, 1, 1), False)
    plans = salary_plans(db, user)
    rates = [p.tax_rate for p in plans]
    assert rates == [D("13")] * 4 + [D("15")] * 6 + [D("18")] * 2  # 5M к ноябрю
    # 5-й месяц: база 2 000 000 → 400 000 по 13% + 100 000 по 15%
    assert plans[3].amount_net == D("435000.00")
    assert plans[4].amount_net == D("433000.00")
    assert plans[5].amount_net == D("425000.00")


def test_salary_calc_base_includes_bonus(db, user, refs):
    _income(db, user, refs.bonus_kind, "2400000", date(2026, 1, 1))
    set_profile(db, user, salary=D("100000"), salary_day=10)
    plan_svc.calculate_salary(db, user, date(2026, 1, 1), False)
    assert all(p.tax_rate == D("15") for p in salary_plans(db, user))
