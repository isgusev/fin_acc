"""Прогноз накоплений по месяцам."""

from datetime import date
from decimal import Decimal

from app import schemas
from app.services import accounts as accounts_svc
from app.services import forecast as forecast_svc

D = Decimal
TODAY = date(2026, 10, 15)


def _account(db, user, refs, code, name, period="none", amount=None):
    return accounts_svc.create_account(
        db,
        user,
        schemas.AccountIn(
            name=name,
            account_type_id=refs.account_types[code],
            replenish_period=period,
            replenish_amount=D(amount) if amount else None,
        ),
    )


def _scenario(db, user, refs, make):
    current = make.account(user, "current", "Карта")
    savings = _account(db, user, refs, "savings", "Подушка")
    monthly = _account(db, user, refs, "monthly", "Продукты", "monthly", "20000")
    fund = _account(db, user, refs, "fund", "Отпуск", "quarterly", "6000")  # 2000 в месяц
    make.transfer(user, current, savings, "100000", on=date(2026, 10, 1))
    make.transfer(user, current, monthly, "8000", on=date(2026, 10, 2))  # уже пополнено

    make.income_plan(user, "50000", on=date(2026, 10, 25))
    paid = make.income_plan(user, "40000", on=date(2026, 10, 10))
    make.income(user, "40000", plan=paid, on=date(2026, 10, 10))  # есть факт — не учитываем
    make.income_plan(user, "10000", on=date(2026, 9, 30))  # просрочен — в текущем месяце
    make.income_plan(user, "60000", on=date(2026, 11, 10))

    make.expense_plan(user, "5000", on=date(2026, 9, 20), funding_account_id=savings.id)
    make.expense_plan(user, "3000", on=date(2026, 10, 28))  # без источника — из накоплений
    make.expense_plan(user, "4000", on=date(2026, 10, 29), funding_account_id=fund.id)  # нет
    make.expense_plan(user, "1000", on=date(2026, 10, 29), funding_account_id=current.id)  # нет
    make.expense_plan(user, "7000", on=date(2026, 11, 5), funding_account_id=savings.id)
    return savings, monthly


def test_forecast_months(db, user, refs, make):
    _scenario(db, user, refs, make)
    fc = forecast_svc.build(db, user, TODAY)
    assert fc.start_balance == D("100000.00")
    assert len(fc.rows) == 12
    oct_, nov, dec = fc.rows[0], fc.rows[1], fc.rows[2]

    assert oct_.is_current and oct_.label == "Октябрь 2026"
    assert oct_.income == D("60000.00")  # 50 000 + просроченные 10 000
    assert oct_.to_monthly == D("12000.00")  # 20 000 − уже пополнено 8 000
    assert oct_.to_funds == D("2000.00")
    assert oct_.expenses == D("8000.00")  # просроченные 5 000 + 3 000 без источника
    assert (oct_.change, oct_.balance) == (D("38000.00"), D("138000.00"))

    assert (nov.income, nov.to_monthly, nov.to_funds, nov.expenses) == (
        D("60000.00"),
        D("20000.00"),
        D("2000.00"),
        D("7000.00"),
    )
    assert nov.balance == D("169000.00")
    assert not nov.after_last_income and dec.after_last_income
    assert fc.last_income_month == date(2026, 11, 1)
    assert dec.balance == D("147000.00")  # только отчисления: −22 000


def test_forecast_uses_future_plan_change(db, user, refs, make):
    _, monthly = _scenario(db, user, refs, make)
    accounts_svc.update_account(
        db,
        user,
        monthly.id,
        schemas.AccountIn(
            name="Продукты",
            account_type_id=refs.account_types["monthly"],
            replenish_period="monthly",
            replenish_amount=D("30000"),
            plan_effective_from=date(2026, 12, 1),
        ),
    )
    rows = forecast_svc.build(db, user, TODAY).rows
    assert (rows[1].to_monthly, rows[2].to_monthly) == (D("20000.00"), D("30000.00"))


def test_forecast_page_and_dashboard_line(user_client, db, user, refs, make):
    _scenario(db, user, refs, make)
    page = user_client.get("/forecast")
    assert page.status_code == 200 and "Прогноз накоплений" in page.text
    nav = user_client.get("/").text.split('id="main-nav"')[1].split("</nav>")[0]
    assert 'href="/forecast"' in nav
    assert "Прогноз накоплений на 31.12." in user_client.get("/").text


def test_forecast_no_rounding_drift(db, user, refs):
    """Квартальные 5000 → по 1666,67 в месяц, но за квартал ровно 5000, без «,01»."""
    _account(db, user, refs, "fund", "Отпуск", "quarterly", "5000")
    rows = forecast_svc.build(db, user, date(2026, 1, 15)).rows
    assert rows[0].to_funds == D("1666.67")
    assert rows[2].balance == D("-5000.00")
    assert rows[11].balance == D("-20000.00")
