"""Импорт банковской выписки: разбор, подсказки по прошлым операциям, сохранение.

Разобранная выписка в БД не хранится: таблица для проверки живёт в форме, а в базу
пишутся только итоговые операции (с исходным описанием из банка — по нему при следующих
импортах подсказываются наименование, счёт и категория).
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schemas
from app.errors import ValidationAppError
from app.models import Operation, User
from app.services import operations as ops_svc
from app.services import statement_tbank

# Сколько последних импортированных операций учитывать при подсказках
HISTORY_LIMIT = 5000


def normalize(description: str) -> str:
    return " ".join(description.split()).lower()


def merchant_key(description: str) -> str | None:
    """«Оплата в PYATEROCHKA 20280 Sovkhoz» → «pyaterochka»: магазин без номера и адреса.

    Позволяет узнать сеть, даже если у конкретного магазина другое описание.
    """
    m = re.match(r"оплата в\s+([^\s\d]+)", normalize(description))
    return m.group(1) if m else None


@dataclass
class Suggestion:
    name: str | None = None
    account_id: uuid.UUID | None = None
    category_id: int | None = None
    source: str | None = None  # «описание» / «магазин» / «карта»


@dataclass
class ImportRow:
    op_date: date
    amount: Decimal  # положительная сумма расхода
    description: str
    card: str | None
    suggestion: Suggestion = field(default_factory=Suggestion)


@dataclass
class PreparedImport:
    rows: list[ImportRow]
    total_lines: int  # всего разобрано записей
    skipped_income: int  # поступления (не показываются)
    skipped_duplicates: int  # уже внесены ранее
    unparsed: int  # похожи на операции, но не разобраны


def _history(db: Session, user: User) -> Sequence[Operation]:
    return db.scalars(
        select(Operation)
        .where(Operation.owner_id == user.id, Operation.bank_description.is_not(None))
        .order_by(Operation.op_date.desc(), Operation.created_at.desc())
        .limit(HISTORY_LIMIT)
    ).all()


def prepare(db: Session, user: User, text: str) -> PreparedImport:
    if not text.strip():
        raise ValidationAppError("Вставьте текст выписки", "text")
    parsed = statement_tbank.parse(text)
    if not parsed.lines:
        raise ValidationAppError(
            "Не удалось распознать ни одной операции. Скопируйте выписку Т-Банка целиком, "
            "вместе с датами и суммами",
            "text",
        )

    history = _history(db, user)
    by_desc: dict[str, Operation] = {}
    by_merchant: dict[str, Operation] = {}
    by_card: dict[str, Operation] = {}
    existing: set[tuple[str, date, Decimal]] = set()
    for op in history:  # от новых к старым — побеждает самая свежая
        if op.bank_description is None:
            continue
        key = normalize(op.bank_description)
        by_desc.setdefault(key, op)
        if (mk := merchant_key(op.bank_description)) is not None:
            by_merchant.setdefault(mk, op)
        if op.bank_card and not op.account.is_closed:
            by_card.setdefault(op.bank_card, op)
        existing.add((key, op.op_date, op.amount))

    rows: list[ImportRow] = []
    income = duplicates = 0
    for line in parsed.lines:
        if not line.is_debit:
            income += 1
            continue
        amount = -line.amount
        key = normalize(line.description)
        if (key, line.op_date, amount) in existing:
            duplicates += 1
            continue
        rows.append(
            ImportRow(
                op_date=line.op_date,
                amount=amount,
                description=line.description,
                card=line.card,
                suggestion=_suggest(line, key, by_desc, by_merchant, by_card),
            )
        )
    return PreparedImport(
        rows=rows,
        total_lines=len(parsed.lines),
        skipped_income=income,
        skipped_duplicates=duplicates,
        unparsed=parsed.unparsed,
    )


def _suggest(
    line: statement_tbank.StatementLine,
    key: str,
    by_desc: dict[str, Operation],
    by_merchant: dict[str, Operation],
    by_card: dict[str, Operation],
) -> Suggestion:
    """Наименование, счёт и категория из прошлой операции с тем же описанием или магазином;
    если таких нет — только счёт, использованный ранее для этой карты."""
    mk = merchant_key(line.description)
    source, op = None, by_desc.get(key)
    if op is not None:
        source = "описание"
    elif mk is not None and (op := by_merchant.get(mk)) is not None:
        source = "магазин"
    if op is not None:
        account_id = None if op.account.is_closed else op.account_id
        category_id = op.category_id if op.category is None or op.category.is_active else None
        return Suggestion(op.name, account_id, category_id, source)
    if line.card and (card_op := by_card.get(line.card)) is not None:
        return Suggestion(account_id=card_op.account_id, source="карта")
    return Suggestion()


def save(db: Session, user: User, rows: Sequence[schemas.ImportRowIn]) -> list[Operation]:
    """Создаёт по расходу на каждую строку. Всё или ничего (ошибки — по строкам)."""
    if not rows:
        raise ValidationAppError("Нет строк для сохранения")
    items = [
        (
            schemas.OperationIn(
                operation_type="expense",
                account_id=r.account_id,
                op_date=r.op_date,
                name=r.name,
                amount=r.amount,
                category_id=r.category_id,
                comment=r.comment,
                plan_id=r.plan_id,
            ),
            r.description,
            r.card,
        )
        for r in rows
    ]
    return ops_svc.create_expenses_bulk(db, user, items)
