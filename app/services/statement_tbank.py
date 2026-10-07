"""Разбор текста выписки Т-Банка (скопированного из PDF-выписки).

Формат: шапка с названиями колонок, затем записи без разделителей:

    28.09.2026 04:23 29.09.2026 16:54 -668.23 ₽ -668.23 ₽ Оплата в VKUSVILL Moskva RUS 8008

— дата и время операции, дата и время списания, сумма в валюте операции, сумма в валюте
карты, описание (может переноситься на несколько строк) и номер карты (4 цифры или «—»).
Переносы строк не важны: текст склеивается в одну строку и разбирается регулярным
выражением; конец описания определяется по номеру карты перед началом следующей записи.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

MAX_DESCRIPTION = 400  # символов описания в одной записи (у Т-Банка — заметно меньше)

_DT = r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2})"
# неразрывные пробелы (NBSP, NNBSP) встречаются в суммах при копировании из PDF
_SPACES = " \u00a0\u202f"
_AMOUNT = r"([+\-−–]\s?\d[\d" + _SPACES + r"]*[.,]\d{2})\s*₽"
_RECORD = re.compile(
    _DT
    + r"\s+"
    + _DT
    + r"\s+"
    + _AMOUNT
    + r"\s+"
    + _AMOUNT
    # длина описания ограничена: без этого на «битом» тексте поиск растёт квадратично
    + rf"\s+(.{{1,{MAX_DESCRIPTION}}}?)\s+(\d{{4}}|[—–\-]{{1,3}})"
    + r"(?=\s+\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}\s+\d{2}\.\d{2}\.\d{4}|\s*$)"
)
# Начало записи — две пары «дата время» подряд; по ним считаем, сколько записей в тексте
_RECORD_START = re.compile(_DT + r"\s+" + _DT + r"\s+[+\-−–]")


@dataclass(frozen=True)
class StatementLine:
    op_date: date
    op_time: time
    amount: Decimal  # со знаком: «−» — списание, «+» — поступление (в валюте карты)
    description: str
    card: str | None  # последние 4 цифры карты или None («—»)

    @property
    def is_debit(self) -> bool:
        return self.amount < 0


@dataclass(frozen=True)
class ParseResult:
    lines: list[StatementLine]
    unparsed: int  # записей, которые выглядят как операции, но не разобраны


def _amount(raw: str) -> Decimal:
    cleaned = re.sub(r"[\s" + _SPACES + "]", "", raw).replace("−", "-").replace("–", "-")
    return Decimal(cleaned.replace(",", "."))


def parse(text: str) -> ParseResult:
    flat = " ".join(text.split())
    lines: list[StatementLine] = []
    for m in _RECORD.finditer(flat):
        op_d, op_t, _, _, _, card_amount, description, card = m.groups()
        try:
            dt = datetime.strptime(f"{op_d} {op_t}", "%d.%m.%Y %H:%M")
            amount = _amount(card_amount)
        except (ValueError, InvalidOperation):
            continue
        lines.append(
            StatementLine(
                op_date=dt.date(),
                op_time=dt.time(),
                amount=amount,
                description=description.strip()[:300],
                card=card if card.isdigit() else None,
            )
        )
    expected = len(_RECORD_START.findall(flat))
    return ParseResult(lines=lines, unparsed=max(expected - len(lines), 0))
