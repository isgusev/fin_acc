"""Чистые функции расчётов: налог, рабочие дни, аванс."""

from datetime import date
from decimal import Decimal

import pytest

from app.services.calc import (
    Bracket,
    advance_amount,
    clamp_day,
    holiday_dates,
    progressive_tax,
    workdays_between,
)

D = Decimal

NDFL = [
    Bracket(D("0"), D("2400000"), D("13")),
    Bracket(D("2400000"), D("5000000"), D("15")),
    Bracket(D("5000000"), D("20000000"), D("18")),
    Bracket(D("20000000"), D("50000000"), D("20")),
    Bracket(D("50000000"), None, D("22")),
]

JAN_HOLIDAYS = holiday_dates([(date(2026, 1, 1), date(2026, 1, 11))])


# ---------------------------------------------------------------- progressive_tax


def test_tax_single_bracket():
    r = progressive_tax(D("0"), D("100000"), NDFL)
    assert r.tax == D("13000.00")
    assert r.net == D("87000.00")
    assert r.max_rate == D("13")


def test_tax_crosses_one_threshold():
    r = progressive_tax(D("2300000"), D("200000"), NDFL)
    # 100 000 по 13% + 100 000 по 15%
    assert r.tax == D("28000.00")
    assert r.net == D("172000.00")
    assert r.max_rate == D("15")


def test_tax_crosses_several_thresholds():
    r = progressive_tax(D("0"), D("6000000"), NDFL)
    # 2.4M*13% + 2.6M*15% + 1M*18%
    assert r.tax == D("312000") + D("390000") + D("180000")
    assert r.max_rate == D("18")


def test_tax_top_bracket_without_upper_bound():
    r = progressive_tax(D("60000000"), D("1000000"), NDFL)
    assert r.tax == D("220000.00")
    assert r.max_rate == D("22")


def test_tax_ending_exactly_on_boundary_stays_in_lower_bracket():
    r = progressive_tax(D("2300000"), D("100000"), NDFL)
    assert r.tax == D("13000.00")
    assert r.max_rate == D("13")


def test_tax_starting_exactly_on_boundary_uses_upper_bracket():
    r = progressive_tax(D("2400000"), D("100000"), NDFL)
    assert r.tax == D("15000.00")
    assert r.max_rate == D("15")


def test_tax_uncovered_gap_is_untaxed():
    scale = [Bracket(D("0"), D("100"), D("10")), Bracket(D("200"), None, D("20"))]
    r = progressive_tax(D("0"), D("300"), scale)
    assert r.tax == D("30.00")  # 10 + 0 (разрыв) + 20
    assert r.net == D("270.00")
    assert r.max_rate == D("20")


def test_tax_entirely_in_gap():
    scale = [Bracket(D("0"), D("100"), D("10")), Bracket(D("200"), None, D("20"))]
    r = progressive_tax(D("100"), D("50"), scale)
    assert r.tax == D("0.00")
    assert r.net == D("50.00")
    assert r.max_rate == D("0")


def test_tax_max_rate_is_highest_applied_even_if_unsorted():
    scale = [Bracket(D("100"), None, D("5")), Bracket(D("0"), D("100"), D("30"))]
    r = progressive_tax(D("0"), D("200"), scale)
    assert r.tax == D("35.00")
    assert r.max_rate == D("30")


def test_tax_rounding_to_kopecks():
    r = progressive_tax(D("0"), D("100.05"), NDFL)
    assert r.tax == D("13.01")  # 13.0065 → 13.01
    assert r.net == D("87.04")


def test_tax_zero_amount():
    r = progressive_tax(D("1000"), D("0"), NDFL)
    assert r.tax == D("0.00")
    assert r.net == D("0.00")


# ---------------------------------------------------------------- рабочие дни


def test_holiday_dates_expands_ranges():
    days = holiday_dates(
        [(date(2026, 1, 1), date(2026, 1, 3)), (date(2026, 5, 1), date(2026, 5, 1))]
    )
    assert days == {date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 5, 1)}


def test_workdays_full_january_2026_with_holidays():
    assert workdays_between(date(2026, 1, 1), date(2026, 1, 31), set()) == 22
    assert workdays_between(date(2026, 1, 1), date(2026, 1, 31), JAN_HOLIDAYS) == 15


def test_workdays_inclusive_bounds_and_weekends():
    # пн 12.01 – пт 16.01
    assert workdays_between(date(2026, 1, 12), date(2026, 1, 16), set()) == 5
    # сб–вс
    assert workdays_between(date(2026, 1, 17), date(2026, 1, 18), set()) == 0
    # один рабочий день
    assert workdays_between(date(2026, 1, 12), date(2026, 1, 12), set()) == 1
    # праздник в будний день исключается
    assert workdays_between(date(2026, 2, 23), date(2026, 2, 27), {date(2026, 2, 23)}) == 4


def test_workdays_reversed_range_is_zero():
    assert workdays_between(date(2026, 1, 20), date(2026, 1, 10), set()) == 0


# ---------------------------------------------------------------- clamp_day


@pytest.mark.parametrize(
    ("year", "month", "day", "expected"),
    [
        (2026, 2, 31, date(2026, 2, 28)),
        (2026, 2, 29, date(2026, 2, 28)),
        (2028, 2, 31, date(2028, 2, 29)),  # високосный
        (2026, 4, 31, date(2026, 4, 30)),
        (2026, 6, 31, date(2026, 6, 30)),
        (2026, 1, 31, date(2026, 1, 31)),
        (2026, 3, 15, date(2026, 3, 15)),
    ],
)
def test_clamp_day(year, month, day, expected):
    assert clamp_day(year, month, day) == expected


# ---------------------------------------------------------------- аванс


def test_advance_january_2026_with_new_year_holidays():
    # 15 рабочих дней в январе, по 15-е включительно — 4 (12..15)
    assert advance_amount(D("300000"), 2026, 1, 15, JAN_HOLIDAYS) == D("80000.00")


def test_advance_calc_day_beyond_month_end_is_clamped():
    # 31 → 28 февраля: отработан весь месяц → аванс = вся зарплата
    assert advance_amount(D("100000"), 2026, 2, 31, set()) == D("100000.00")


def test_advance_rounded_to_kopecks():
    # февраль 2026: 20 рабочих дней, 23.02 праздник → 19; по 15-е — 10
    holidays = {date(2026, 2, 23)}
    assert advance_amount(D("300000"), 2026, 2, 15, holidays) == D("157894.74")


def test_advance_month_without_workdays_is_zero():
    all_month = holiday_dates([(date(2026, 1, 1), date(2026, 1, 31))])
    assert advance_amount(D("100000"), 2026, 1, 15, all_month) == D("0.00")
