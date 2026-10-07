"""Импорт выписки Т-Банка: разбор текста, подсказки, дубли, сохранение, API и веб."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app import schemas
from app.errors import RowsValidationError, ValidationAppError
from app.services import references
from app.services import statement_import as import_svc
from app.services.balances import account_balance
from app.services.statement_tbank import parse
from tests.conftest import API

D = Decimal
SAMPLE = (Path(__file__).parent / "data" / "tbank_statement.txt").read_text(encoding="utf-8")


def record(desc: str, amount: str = "-100.00", card: str = "8008", d: str = "01.10.2026") -> str:
    return f"{d}\n12:00\n{d}\n12:05\n{amount} ₽ {amount} ₽ {desc}\n{card}\n"


# ---------------------------------------------------------------- разбор текста


def test_parse_sample_statement():
    r = parse(SAMPLE)
    assert r.unparsed == 0
    got = [(x.op_date, x.amount, x.card, x.description) for x in r.lines]
    assert got == [
        (date(2026, 10, 6), D("34540.24"), "8008", "Перевод себе"),
        (date(2026, 10, 5), D("-99.00"), None, "Плата за оповещения об операциях"),
        (date(2026, 10, 5), D("16.10"), None, "Кэшбэк за покупки в Городе"),
        (date(2026, 10, 5), D("36.10"), None, "Кэшбэк за покупки у партнеров"),
        (date(2026, 9, 28), D("-668.23"), "8008", "Оплата в VKUSVILL Moskva RUS"),
        (date(2026, 9, 28), D("1000.00"), "8008", "Перевод себе"),
        (
            date(2026, 9, 26),
            D("-1922.31"),
            "7295",
            "Внешний перевод по номеру телефона +79990000000",
        ),
        (date(2026, 9, 25), D("-147.96"), "7295", 'Оплата в PYATEROCHKA 20280 Sovkhoz "Pobe RUS'),
    ]


def test_parse_uses_operation_date_not_debit_date():
    (line,) = parse("28.09.2026\n04:23\n29.09.2026\n16:54\n-1.00 ₽ -1.00 ₽ X\n8008").lines
    assert line.op_date == date(2026, 9, 28)


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("-1 234.50", D("-1234.50")),  # неразрывный пробел из PDF
        ("-1 234,50", D("-1234.50")),  # узкий неразрывный пробел и запятая
        ("−99.00", D("-99.00")),  # типографский минус
        ("+12 000.00", D("12000.00")),
    ],
)
def test_parse_amount_formats(amount, expected):
    (line,) = parse(record("Оплата в SHOP", amount)).lines
    assert line.amount == expected


@pytest.mark.parametrize("card", ["—", "---", "-"])
def test_parse_card_dash_means_no_card(card):
    (line,) = parse(record("Комиссия", card=card)).lines
    assert line.card is None


def test_description_ending_with_digits_is_not_confused_with_card():
    text = record("Оплата в SHOP 1234\nMoskva 5678", card="7295") + record("Оплата в CAFE")
    a, b = parse(text).lines
    assert (a.description, a.card) == ("Оплата в SHOP 1234 Moskva 5678", "7295")
    assert b.description == "Оплата в CAFE"


def test_parse_counts_broken_records():
    broken = "01.10.2026 12:00 01.10.2026 12:05 -abc ₽ сломано\n"
    r = parse(record("Оплата в A") + broken + record("Оплата в B"))
    assert [x.description for x in r.lines] == ["Оплата в A", "Оплата в B"]
    assert r.unparsed == 1


# ---------------------------------------------------------------- подготовка таблицы


def test_prepare_requires_recognizable_text(db, user):
    for text in ("", "   ", "просто текст без операций"):
        with pytest.raises(ValidationAppError) as ei:
            import_svc.prepare(db, user, text)
        assert ei.value.field == "text"


def test_prepare_shows_only_debits_as_positive_amounts(db, user):
    p = import_svc.prepare(db, user, SAMPLE)
    assert (p.total_lines, p.skipped_income, p.skipped_duplicates) == (8, 4, 0)
    assert [r.amount for r in p.rows] == [D("99.00"), D("668.23"), D("1922.31"), D("147.96")]
    assert all(r.suggestion.source is None for r in p.rows)  # история пуста


def _row(**kw):
    data = {
        "op_date": date(2026, 9, 28),
        "amount": D("668.23"),
        "description": "Оплата в VKUSVILL Moskva RUS",
        "card": "8008",
        "name": "ВкусВилл",
    } | kw
    return schemas.ImportRowIn(**data)


def test_save_creates_expenses_with_bank_fields(db, user, make, refs):
    card = make.account(user, "current", "Карта")
    ops = import_svc.save(
        db,
        user,
        [_row(account_id=card.id, category_id=refs.categories["Еда"], comment="к чаю")],
    )
    (op,) = ops
    assert (op.operation_type.code, op.amount, op.name) == ("expense", D("668.23"), "ВкусВилл")
    assert (op.bank_description, op.bank_card, op.comment) == (
        "Оплата в VKUSVILL Moskva RUS",
        "8008",
        "к чаю",
    )


def test_save_is_all_or_nothing_with_row_errors(db, user, make, refs):
    card = make.account(user, "current", "Карта")
    rows = [
        _row(account_id=card.id, category_id=refs.misc_category),
        _row(account_id=card.id, name=None, category_id=refs.misc_category),  # нет наименования
        _row(account_id=card.id),  # «Текущий» без категории
        _row(account_id=None, category_id=refs.misc_category),  # нет счёта
    ]
    with pytest.raises(RowsValidationError) as ei:
        import_svc.save(db, user, rows)
    assert {i: [f for f, _ in errs] for i, errs in ei.value.row_errors.items()} == {
        1: ["name"],
        2: ["category_id"],
        3: ["account_id"],
    }
    db.rollback()
    assert account_balance(db, user.id, card.id) == D("0.00")  # не сохранено ничего


def test_prepare_suggests_from_history_and_skips_duplicates(db, user, make, refs):
    card = make.account(user, "current", "Карта")
    monthly = make.account(user, "monthly", "Продукты")
    import_svc.save(
        db,
        user,
        [
            _row(account_id=monthly.id, category_id=refs.categories["Еда"]),  # ВкусВилл
            _row(
                description="Оплата в PYATEROCHKA 11111 Moskva RUS",
                op_date=date(2026, 8, 1),
                amount=D("50"),
                card="7295",
                name="Пятёрочка",
                account_id=card.id,
                category_id=refs.categories["Еда"],
            ),
        ],
    )
    text = SAMPLE + record("Оплата в VKUSVILL Moskva RUS", "-10.00", d="07.10.2026")
    p = import_svc.prepare(db, user, text)
    assert p.skipped_duplicates == 1  # ВкусВилл 28.09 на 668.23 уже внесён
    by_desc = {r.description: r for r in p.rows}

    vkus = by_desc["Оплата в VKUSVILL Moskva RUS"]  # новая покупка — по точному описанию
    assert (vkus.amount, vkus.suggestion.source) == (D("10.00"), "описание")
    assert (vkus.suggestion.name, vkus.suggestion.account_id) == ("ВкусВилл", monthly.id)
    assert vkus.suggestion.category_id == refs.categories["Еда"]

    pyat = by_desc['Оплата в PYATEROCHKA 20280 Sovkhoz "Pobe RUS']  # другой магазин сети
    assert (pyat.suggestion.source, pyat.suggestion.name) == ("магазин", "Пятёрочка")
    assert pyat.suggestion.account_id == card.id

    phone = by_desc["Внешний перевод по номеру телефона +79990000000"]  # только счёт по карте
    assert (phone.suggestion.source, phone.suggestion.account_id) == ("карта", card.id)
    assert phone.suggestion.name is None

    fee = by_desc["Плата за оповещения об операциях"]  # без карты и истории
    assert fee.suggestion.source is None


def test_suggestion_skips_closed_account_and_inactive_category(db, user, make, refs):
    monthly = make.account(user, "monthly", "Продукты")
    import_svc.save(db, user, [_row(account_id=monthly.id, category_id=refs.categories["Еда"])])
    monthly.is_closed = True  # закрыть штатно нельзя: остаток ненулевой
    db.flush()
    references.update_expense_category(
        db, refs.categories["Еда"], schemas.ExpenseCategoryIn(name="Еда", is_active=False)
    )
    p = import_svc.prepare(db, user, record("Оплата в VKUSVILL Moskva RUS", "-1.00"))
    (row,) = p.rows
    assert row.suggestion.name == "ВкусВилл"
    assert row.suggestion.account_id is None and row.suggestion.category_id is None


def test_history_is_per_user(db, user, other_user, make, refs):
    acc = make.account(user, "monthly", "Продукты")
    import_svc.save(db, user, [_row(account_id=acc.id)])
    p = import_svc.prepare(db, other_user, SAMPLE)
    assert p.skipped_duplicates == 0
    assert all(r.suggestion.source is None for r in p.rows)


# ---------------------------------------------------------------- API и веб


def test_import_api_parse_and_save(user_client):
    r = user_client.post(f"{API}/import/tbank/parse", json={"text": SAMPLE})
    assert r.status_code == 200
    body = r.json()
    assert (body["total"], len(body["rows"])) == (8, 4)
    assert body["rows"][1]["amount"] == "668.23"

    refs = user_client.get(f"{API}/references").json()
    monthly = next(t["id"] for t in refs["account_types"] if t["code"] == "monthly")
    acc = user_client.post(
        f"{API}/accounts", json={"name": "Продукты", "account_type_id": monthly}
    ).json()
    row = {k: body["rows"][1][k] for k in ("op_date", "amount", "description", "card")}
    bad = user_client.post(f"{API}/import/save", json={"rows": [row]})
    assert bad.status_code == 422
    err = bad.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert {e["field"] for e in err["rows"]["0"]} == {"account_id"}

    ok = user_client.post(
        f"{API}/import/save", json={"rows": [row | {"name": "ВкусВилл", "account_id": acc["id"]}]}
    )
    assert ok.status_code == 201
    assert ok.json()[0]["bank_description"] == "Оплата в VKUSVILL Moskva RUS"
    again = user_client.post(f"{API}/import/tbank/parse", json={"text": SAMPLE}).json()
    assert again["skipped_duplicates"] == 1


def test_import_web_flow(db, user_client, user, make, refs):
    card = make.account(user, "current", "Карта")
    page = user_client.post("/import/parse", data={"text": SAMPLE})
    assert page.status_code == 200
    html = page.text
    assert "Проверка выписки" in html and "VKUSVILL" in html
    assert "Пишем в базу?" in html
    assert "Перевод себе" not in html  # поступления не показываются

    base = {
        "rows-0-op_date": "2026-09-28",
        "rows-0-amount": "668.23",
        "rows-0-description": "Оплата в VKUSVILL Moskva RUS",
        "rows-0-card": "8008",
        "rows-0-account_id": str(card.id),
        "rows-0-category_id": str(refs.categories["Еда"]),
        "rows-0-comment": "",
        # строка 1 «удалена» в браузере — её полей в форме нет; строка 2 сохраняется
        "rows-2-op_date": "2026-09-25",
        "rows-2-amount": "147.96",
        "rows-2-description": "Оплата в PYATEROCHKA",
        "rows-2-card": "7295",
        "rows-2-name": "Пятёрочка",
        "rows-2-account_id": str(card.id),
        "rows-2-category_id": str(refs.categories["Еда"]),
    }
    bad = user_client.post("/import/save", data=base | {"rows-0-name": ""})
    assert bad.status_code == 422
    assert "Ничего не сохранено" in bad.text
    assert "Для расхода обязательно укажите наименование" in bad.text

    ok = user_client.post(
        "/import/save", data=base | {"rows-0-name": "ВкусВилл"}, follow_redirects=False
    )
    assert ok.status_code == 303
    assert account_balance(db, user.id, card.id) == D("-816.19")  # 668.23 + 147.96
