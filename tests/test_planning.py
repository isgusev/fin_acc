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
    # профиль, заполненный до появления проверок, отвергается и при расчёте
    user.salary, user.salary_day, user.advance_day, user.advance_calc_day = (
        D("100000"),
        10,
        25,
        None,
    )
    db.flush()
    with pytest.raises(ValidationAppError) as ei:
        plan_svc.calculate_salary(db, user, date(2026, 1, 1), False)
    assert ei.value.field == "advance_calc_day"


@pytest.mark.parametrize(
    ("advance_day", "salary_day", "calc_day", "field"),
    [
        (25, 10, None, "advance_calc_day"),  # аванс без «Расчёта аванса»
        (15, 10, 20, "advance_calc_day"),  # «Расчёт аванса» позже «Даты аванса»
        (25, 25, 15, "advance_day"),  # аванс и зарплата в один день
    ],
)
def test_profile_advance_days_validation(db, user, advance_day, salary_day, calc_day, field):
    with pytest.raises(ValidationAppError) as ei:
        set_profile(
            db,
            user,
            salary="100000",
            advance_day=advance_day,
            salary_day=salary_day,
            advance_calc_day=calc_day,
        )
    assert ei.value.field == field


def test_profile_advance_calc_day_equal_to_advance_day_ok(db, user):
    set_profile(db, user, salary="100000", advance_day=15, salary_day=1, advance_calc_day=15)
    assert user.advance_calc_day == 15


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
    """Аванс 20-го за текущий месяц, зарплата 5-го — остаток за предыдущий месяц."""
    set_profile(db, user, salary=D("300000"), salary_day=5, advance_day=20, advance_calc_day=15)
    r = plan_svc.calculate_salary(db, user, date(2026, 1, 1), False)
    by_date = {p.planned_date: p.amount_planned for p in r.created}
    # 12 авансов + 12 зарплат за дек-2025…ноя-2026 + 05.01.2027 за декабрь 2026
    assert len(r.created) == 25
    # 05.01.2026 — остаток за декабрь 2025: 23 рабочих дня, по 15-е — 11
    assert by_date[date(2026, 1, 5)] == D("156521.74")
    # январь 2026: 15 рабочих дней (1–11 праздники), по 15-е — 4 → аванс 80 000
    assert by_date[date(2026, 1, 20)] == D("80000.00")
    assert by_date[date(2026, 2, 5)] == D("220000.00")  # остаток за январь
    # февраль: 19 рабочих дней (23.02 праздник), по 15-е — 10
    assert by_date[date(2026, 2, 20)] == D("157894.74")
    assert by_date[date(2026, 3, 5)] == D("142105.26")
    for m in range(1, 13):  # аванс за месяц + остаток за него в следующем месяце = оклад
        y, nm = (2027, 1) if m == 12 else (2026, m + 1)
        assert by_date[date(2026, m, 20)] + by_date[date(y, nm, 5)] == D("300000.00")


def test_salary_calc_user_example_november_2026(db, user):
    """Аванс 25-го, зарплата 10-го, «Расчёт аванса» 15, оклад 100 000, учёт с 01.11.2026."""
    set_profile(db, user, salary="100000", advance_day=25, salary_day=10, advance_calc_day=15)
    r = plan_svc.calculate_salary(db, user, date(2026, 11, 1), False)
    got = {p.planned_date: p.amount_planned for p in r.created}
    # октябрь 2026: 22 рабочих дня, по 15-е — 11 → остаток 50 000 выплачивается 10.11
    assert got[date(2026, 11, 10)] == D("50000.00")
    assert got[date(2026, 11, 25)] == D("45000.00")  # аванс за ноябрь: 100000 / 20 × 9
    assert got[date(2026, 12, 10)] == D("55000.00")  # остаток за ноябрь: 100000 / 20 × 11
    # декабрь: 22 рабочих дня (31.12 праздник), по 15-е — 11 → 50 000 / 50 000
    assert got[date(2026, 12, 25)] == D("50000.00")
    assert got[date(2027, 1, 10)] == D("50000.00")  # остаток за декабрь — уже в 2027 году
    assert len(got) == 5
    # выплата января 2027 начинает налоговую базу нового года
    (jan,) = [p for p in r.created if p.planned_date.year == 2027]
    assert jan.tax_rate == D("13.00")


def test_salary_calc_replace_covers_next_january(db, user):
    set_profile(db, user, salary="100000", advance_day=25, salary_day=10, advance_calc_day=15)
    plan_svc.calculate_salary(db, user, date(2026, 12, 1), False)
    r = plan_svc.calculate_salary(db, user, date(2026, 12, 1), True)
    assert (len(r.created), r.replaced, r.skipped) == (3, 3, 0)
    r = plan_svc.calculate_salary(db, user, date(2026, 12, 1), False)
    assert (len(r.created), r.skipped) == (0, 3)


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


# ---------------------------------------------------------------- пример из ТЗ: ноябрь 2026


def test_salary_split_november_2026_example(db, user):
    """Ноябрь 2026: 21 будний день − 04.11 = 20 рабочих; до 15-го включительно 10 − 1 = 9.
    Зарплата 100 000: аванс = 100000 / 20 × 9 = 45 000, зарплата = 100000 / 20 × 11 = 55 000."""
    set_profile(db, user, salary="100000", advance_day=20, salary_day=28, advance_calc_day=15)
    plan_svc.calculate_salary(db, user, date(2026, 11, 1), False)
    nov = {
        p.planned_date: p.amount_planned
        for p in salary_plans(db, user)
        if p.planned_date.month == 11
    }
    assert nov == {date(2026, 11, 20): D("45000.00"), date(2026, 11, 28): D("55000.00")}


# ---------------------------------------------------------------- доход до начала учёта


def test_prior_income_default_zero(db, user):
    assert plan_svc.get_prior_income(db, user, 2026) == D("0")


def test_prior_income_shifts_progressive_base(db, user):
    """База 2 350 000 до начала учёта: аванс 45 000 целиком по 13 %,
    из зарплаты 55 000 — 5 000 по 13 % и 50 000 по 15 %."""
    set_profile(db, user, salary="100000", advance_day=20, salary_day=28, advance_calc_day=15)
    plan_svc.calculate_salary(db, user, date(2026, 11, 1), False, prior_income=D("2350000"))
    assert plan_svc.get_prior_income(db, user, 2026) == D("2350000.00")
    by_date = {p.planned_date: p for p in salary_plans(db, user)}
    adv, pay = by_date[date(2026, 11, 20)], by_date[date(2026, 11, 28)]
    assert (adv.tax_rate, adv.amount_net) == (D("13.00"), D("39150.00"))
    assert (pay.tax_rate, pay.amount_net) == (D("15.00"), D("46850.00"))  # 55000 − 650 − 7500


def test_set_prior_income_recomputes_existing_plans(db, user):
    set_profile(db, user, salary="100000", salary_day=28)
    plan_svc.calculate_salary(db, user, date(2026, 12, 1), False)
    (dec,) = salary_plans(db, user)
    assert dec.tax_rate == D("13.00")

    plan_svc.set_prior_income(db, user, 2026, D("2400000"))
    db.refresh(dec)
    assert (dec.tax_rate, dec.amount_net) == (D("15.00"), D("85000.00"))

    plan_svc.set_prior_income(db, user, 2026, D("0"))  # обновление существующей записи
    db.refresh(dec)
    assert dec.tax_rate == D("13.00")


def test_prior_income_is_per_year_and_per_user(db, user, other_user):
    plan_svc.set_prior_income(db, user, 2026, D("500000"))
    assert plan_svc.get_prior_income(db, user, 2025) == D("0")
    assert plan_svc.get_prior_income(db, other_user, 2026) == D("0")


def test_calculate_salary_without_prior_income_keeps_saved_value(db, user):
    set_profile(db, user, salary="100000", salary_day=28)
    plan_svc.set_prior_income(db, user, 2026, D("700000"))
    plan_svc.calculate_salary(db, user, date(2026, 12, 1), True)
    assert plan_svc.get_prior_income(db, user, 2026) == D("700000.00")


# ---------------------------------------------------------------- налоги: проверки из ТЗ


def test_huge_bonus_crosses_two_brackets(db, user, refs):
    """База 2 300 000, премия 3 000 000: 100 000 × 13 % + 2 600 000 × 15 % + 300 000 × 18 %."""
    plan_svc.set_prior_income(db, user, 2026, D("2300000"))
    bonus = plan_svc.create_plan(
        db,
        user,
        plan_in(
            operation_type="income",
            income_kind_id=refs.bonus_kind,
            amount_planned=D("3000000"),
            is_taxable=True,
            planned_date=date(2026, 6, 1),
        ),
    )
    tax = D("13000") + D("390000") + D("54000")
    assert bonus.tax_rate == D("18.00")
    assert bonus.amount_net == D("3000000") - tax


def test_mid_year_bonus_recomputes_later_payments_only(db, user, refs):
    set_profile(db, user, salary="300000", salary_day=10)
    plan_svc.calculate_salary(db, user, date(2026, 1, 1), False)

    def rates():
        return {p.planned_date: p.tax_rate for p in salary_plans(db, user)}

    before = rates()
    assert before[date(2026, 7, 10)] == D("13.00")  # база 1 800 000

    bonus = plan_svc.create_plan(
        db,
        user,
        plan_in(
            operation_type="income",
            income_kind_id=refs.bonus_kind,
            amount_planned=D("1000000"),
            is_taxable=True,
            planned_date=date(2026, 6, 15),
        ),
    )
    # премия: база 1 800 000 → 600 000 по 13 % и 400 000 по 15 %
    assert (bonus.tax_rate, bonus.amount_net) == (D("15.00"), D("862000.00"))
    after = rates()
    for d, rate in after.items():
        if d < date(2026, 6, 15):
            assert rate == before[d]  # выплаты до премии не меняются
    assert after[date(2026, 7, 10)] == D("15.00")  # база 2 800 000
    # 2 800 000 + 5 × 300 000 = 4 300 000; 12.10: 4 300 000 + 300 000 = 4 600 000 < 5 000 000
    assert after[date(2026, 12, 10)] == D("15.00")

    plan_svc.delete_plan(db, user, bonus.id)  # удаление премии возвращает прежние ставки
    assert rates() == before


# ---------------------------------------------------------------- ближайшие траты (главная)


def test_upcoming_expenses_without_fact_sorted_and_limited(db, user, make):
    acc = make.account(user, "monthly")
    overdue = make.expense_plan(user, "100", on=date(2026, 1, 5), name="Просрочено")
    soon = make.expense_plan(user, "200", on=date(2026, 2, 1), name="Скоро")
    later = make.expense_plan(user, "300", on=date(2026, 3, 1), name="Позже")
    done = make.expense_plan(user, "400", on=date(2026, 1, 1), name="Оплачено")
    make.expense(user, acc, "400", plan_id=done.id)  # есть факт — не показываем
    make.income_plan(user, on=date(2026, 1, 2))  # доходы не показываем

    got = plan_svc.upcoming_expenses(db, user, limit=10)
    assert [p.id for p in got] == [overdue.id, soon.id, later.id]
    assert [p.id for p in plan_svc.upcoming_expenses(db, user, limit=2)] == [overdue.id, soon.id]
    assert plan_svc.upcoming_expenses(db, user, limit=0) == []


def test_dashboard_upcoming_block_and_nav(user_client, user, make):
    make.expense_plan(user, "1500", on=date(2026, 1, 5), name="Шиномонтаж")
    html = user_client.get("/").text
    assert "Ближайшие запланированные траты" in html and "Шиномонтаж" in html
    assert "просрочено" in html  # дата в прошлом, факта нет
    nav = html.split('id="main-nav"')[1].split("</nav>")[0]
    assert "/import" not in nav  # импорт — только кнопкой на главной
    assert 'href="/import"' in html
