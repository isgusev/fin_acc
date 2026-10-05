"""Чистые функции расчётов (без БД): прогрессивный налог, рабочие дни, аванс."""

import calendar
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def money(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Bracket:
    income_from: Decimal
    income_to: Decimal | None  # None = без верхней границы
    rate: Decimal  # проценты


@dataclass(frozen=True)
class TaxResult:
    tax: Decimal
    net: Decimal
    max_rate: Decimal  # наибольшая из применённых ставок


def progressive_tax(
    base_before: Decimal, amount: Decimal, brackets: Sequence[Bracket]
) -> TaxResult:
    """Налог на сумму `amount`, если до неё за год уже начислено `base_before`.

    Доход в интервале [base_before, base_before + amount) раскладывается по ступеням шкалы:
    часть, попавшая в ступень, облагается её ставкой. Часть дохода, не покрытая ни одной
    ступенью, налогом не облагается. В max_rate записывается наибольшая применённая ставка.
    """
    if amount <= 0:
        return TaxResult(Decimal("0.00"), money(amount), Decimal("0"))
    lo, hi = base_before, base_before + amount
    tax = Decimal("0")
    max_rate: Decimal | None = None
    for b in sorted(brackets, key=lambda x: x.income_from):
        b_hi = b.income_to if b.income_to is not None else hi
        overlap = min(hi, b_hi) - max(lo, b.income_from)
        if overlap > 0:
            tax += overlap * b.rate / Decimal(100)
            max_rate = b.rate if max_rate is None else max(max_rate, b.rate)
    tax = money(tax)
    return TaxResult(tax=tax, net=money(amount - tax), max_rate=max_rate or Decimal("0"))


def holiday_dates(ranges: Iterable[tuple[date, date]]) -> set[date]:
    days: set[date] = set()
    for d_from, d_to in ranges:
        d = d_from
        while d <= d_to:
            days.add(d)
            d += timedelta(days=1)
    return days


def workdays_between(start: date, end: date, holidays: set[date]) -> int:
    """Количество будних (пн–пт) нерабочих-не-праздничных дней в [start, end] включительно."""
    if end < start:
        return 0
    n = 0
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in holidays:
            n += 1
        d += timedelta(days=1)
    return n


def clamp_day(year: int, month: int, day: int) -> date:
    """Число месяца, ограниченное последним днём месяца (31 → 30 / 28 / 29)."""
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last))


def advance_amount(
    salary: Decimal, year: int, month: int, advance_calc_day: int, holidays: set[date]
) -> Decimal:
    """Аванс = зарплата / рабочих дней в месяце × рабочих дней с 1-го по «Расчёт аванса».

    День «Расчёт аванса» включается в расчёт.
    """
    first = date(year, month, 1)
    last = clamp_day(year, month, 31)
    total = workdays_between(first, last, holidays)
    if total == 0:
        return Decimal("0.00")
    worked = workdays_between(first, clamp_day(year, month, advance_calc_day), holidays)
    return money(salary * worked / total)
