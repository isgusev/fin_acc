"""Расчёт балансов.

Баланс не хранится, а всегда считается по операциям, поэтому изменение
операций «задним числом» автоматически даёт корректные остатки:

    баланс счёта = доходы − расходы − исходящие переводы + входящие переводы
"""

import uuid
from collections.abc import Iterable
from datetime import date
from decimal import Decimal

from sqlalchemy import Subquery, case, func, literal, select, union_all
from sqlalchemy.orm import Session

from app.models import Account, AccountType, Operation, OperationType, OperationTypeCode

ZERO = Decimal("0.00")


def _movements(owner_id: uuid.UUID, as_of: date | None = None) -> Subquery:
    """Подзапрос (account_id, delta): по одной строке на каждое движение по счёту."""
    signed = case(
        (OperationType.code == OperationTypeCode.INCOME.value, Operation.amount),
        else_=-Operation.amount,
    )
    outgoing = (
        select(Operation.account_id.label("account_id"), signed.label("delta"))
        .join(OperationType, OperationType.id == Operation.operation_type_id)
        .where(Operation.owner_id == owner_id)
    )
    incoming = select(
        Operation.target_account_id.label("account_id"), Operation.amount.label("delta")
    ).where(Operation.owner_id == owner_id, Operation.target_account_id.is_not(None))
    if as_of is not None:
        outgoing = outgoing.where(Operation.op_date <= as_of)
        incoming = incoming.where(Operation.op_date <= as_of)
    return union_all(outgoing, incoming).subquery("movements")


def account_balances(
    db: Session,
    owner_id: uuid.UUID,
    account_ids: Iterable[uuid.UUID] | None = None,
    as_of: date | None = None,
) -> dict[uuid.UUID, Decimal]:
    m = _movements(owner_id, as_of)
    q = select(m.c.account_id, func.coalesce(func.sum(m.c.delta), literal(0))).group_by(
        m.c.account_id
    )
    if account_ids is not None:
        ids = list(account_ids)
        if not ids:
            return {}
        q = q.where(m.c.account_id.in_(ids))
    return {row[0]: Decimal(row[1]).quantize(ZERO) for row in db.execute(q)}


def account_balance(db: Session, owner_id: uuid.UUID, account_id: uuid.UUID) -> Decimal:
    return account_balances(db, owner_id, [account_id]).get(account_id, ZERO)


def balances_by_type(db: Session, owner_id: uuid.UUID) -> list[tuple[AccountType, Decimal]]:
    """Сводный баланс по типам счетов (по всем типам, включая типы без счетов → 0)."""
    m = _movements(owner_id)
    per_type: dict[int, Decimal] = {
        row[0]: row[1]
        for row in db.execute(
            select(Account.account_type_id, func.sum(m.c.delta))
            .join(m, m.c.account_id == Account.id)
            .where(Account.owner_id == owner_id)
            .group_by(Account.account_type_id)
        )
    }
    types = db.scalars(select(AccountType).order_by(AccountType.sort_order, AccountType.name)).all()
    return [(t, Decimal(per_type.get(t.id) or 0).quantize(ZERO)) for t in types]
