"""Счета пользователя."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import schemas
from app.errors import ConflictError, NotFoundError, ValidationAppError
from app.models import (
    Account,
    AccountType,
    AccountTypeCode,
    Operation,
    OperationType,
    OperationTypeCode,
    Planning,
    ReplenishPeriod,
    User,
)
from app.services import references
from app.services.balances import ZERO, account_balance, account_balances
from app.services.calc import money

UNALLOCATED_DEFAULT_NAME = "Нераспределённый доход"


def list_accounts(
    db: Session,
    user: User,
    *,
    type_code: str | None = None,
    include_closed: bool = True,
) -> Sequence[Account]:
    q = (
        select(Account)
        .join(AccountType)
        .where(Account.owner_id == user.id)
        .order_by(Account.is_closed, AccountType.sort_order, Account.name)
    )
    if type_code is not None:
        q = q.where(AccountType.code == type_code)
    if not include_closed:
        q = q.where(~Account.is_closed)
    return db.scalars(q).all()


def get_account(
    db: Session, user: User, account_id: uuid.UUID, field: str | None = None
) -> Account:
    """Счёт текущего пользователя. Чужой счёт выглядит как несуществующий."""
    acc = db.get(Account, account_id)
    if acc is None or acc.owner_id != user.id:
        raise NotFoundError("Счёт не найден", field)
    return acc


def get_unallocated_account(db: Session, user: User) -> Account:
    acc = db.scalar(
        select(Account)
        .join(AccountType)
        .where(
            Account.owner_id == user.id,
            AccountType.code == AccountTypeCode.UNALLOCATED.value,
            ~Account.is_closed,
        )
    )
    if acc is None:
        raise ConflictError(
            "Нет открытого счёта с типом «Нераспределённый доход» — создайте его, "
            "чтобы вносить доходы",
            "account_id",
        )
    return acc


def to_out(acc: Account, balance: Decimal) -> schemas.AccountOut:
    out = schemas.AccountOut.model_validate(acc)
    out.balance = balance
    return out


def accounts_with_balances(
    db: Session, user: User, accounts: Sequence[Account]
) -> list[schemas.AccountOut]:
    balances = account_balances(db, user.id, [a.id for a in accounts])
    return [to_out(a, balances.get(a.id, ZERO)) for a in accounts]


def _check_single_per_user(
    db: Session, user: User, at: AccountType, exclude_id: uuid.UUID | None = None
) -> None:
    if not at.single_per_user:
        return
    q = select(Account.id).where(
        Account.owner_id == user.id, Account.account_type_id == at.id, ~Account.is_closed
    )
    if exclude_id is not None:
        q = q.where(Account.id != exclude_id)
    if db.scalar(q.limit(1)) is not None:
        raise ConflictError(
            f"Открытый счёт с типом «{at.name}» уже есть — допускается только один",
            "account_type_id",
        )


def _has_operations(db: Session, account_id: uuid.UUID) -> bool:
    return bool(
        db.scalar(
            select(
                exists().where(
                    or_(
                        Operation.account_id == account_id,
                        Operation.target_account_id == account_id,
                    )
                )
            )
        )
    )


def _commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        if "uq_accounts_owner_name_open" in str(e.orig):
            raise ConflictError("Открытый счёт с таким наименованием уже существует", "name") from e
        raise


# Длительность периода регулярности пополнения в месяцах
PERIOD_MONTHS: dict[ReplenishPeriod, Decimal] = {
    ReplenishPeriod.WEEKLY: Decimal(12) / Decimal(52),
    ReplenishPeriod.MONTHLY: Decimal(1),
    ReplenishPeriod.QUARTERLY: Decimal(3),
    ReplenishPeriod.YEARLY: Decimal(12),
}


def calc_replenish_amount(target: Decimal, months_to_goal: int, period: ReplenishPeriod) -> Decimal:
    """Сумма пополнения = целевая сумма / (месяцев до цели / месяцев в периоде).

    15 000 за 9 месяцев ежеквартально → 15 000 / (9 / 3) = 5 000 в квартал.
    """
    periods = Decimal(months_to_goal) / PERIOD_MONTHS[period]
    return money(target / periods)


def _account_fields(at: AccountType, data: schemas.AccountIn) -> dict[str, object]:
    fields = data.model_dump()
    if data.target_amount is None:
        return fields
    if at.code != AccountTypeCode.FUND.value:
        raise ValidationAppError("Целевая сумма задаётся только для фондов", "target_amount")
    if data.months_to_goal is None:
        raise ValidationAppError(
            "Для расчёта по целевой сумме укажите количество месяцев до цели", "months_to_goal"
        )
    if data.replenish_period == ReplenishPeriod.NONE:
        raise ValidationAppError(
            "Для расчёта по целевой сумме выберите регулярность пополнения", "replenish_period"
        )
    if Decimal(data.months_to_goal) < PERIOD_MONTHS[data.replenish_period]:
        raise ValidationAppError("Срок до цели меньше одного периода пополнения", "months_to_goal")
    fields["replenish_amount"] = calc_replenish_amount(
        data.target_amount, data.months_to_goal, data.replenish_period
    )
    return fields


def create_account(db: Session, user: User, data: schemas.AccountIn) -> Account:
    at = references.get_account_type(db, data.account_type_id)
    _check_single_per_user(db, user, at)
    acc = Account(owner_id=user.id, **_account_fields(at, data))
    db.add(acc)
    _commit(db)
    return acc


def ensure_unallocated_account(db: Session, user: User) -> None:
    """При регистрации каждому пользователю создаётся счёт «Нераспределённый доход»."""
    at = references.account_type_by_code(db, AccountTypeCode.UNALLOCATED.value)
    exists_q = select(Account.id).where(
        Account.owner_id == user.id, Account.account_type_id == at.id, ~Account.is_closed
    )
    if db.scalar(exists_q) is None:
        db.add(Account(owner_id=user.id, name=UNALLOCATED_DEFAULT_NAME, account_type_id=at.id))
        _commit(db)


def update_account(
    db: Session, user: User, account_id: uuid.UUID, data: schemas.AccountIn
) -> Account:
    acc = get_account(db, user, account_id)
    if data.account_type_id != acc.account_type_id:
        if _has_operations(db, acc.id):
            raise ConflictError(
                "Нельзя сменить тип счёта, по которому уже есть операции", "account_type_id"
            )
        at = references.get_account_type(db, data.account_type_id)
        if not acc.is_closed:
            _check_single_per_user(db, user, at, exclude_id=acc.id)
    else:
        at = acc.account_type
    for k, v in _account_fields(at, data).items():
        setattr(acc, k, v)
    _commit(db)
    db.refresh(acc)
    return acc


def delete_account(db: Session, user: User, account_id: uuid.UUID) -> None:
    acc = get_account(db, user, account_id)
    if _has_operations(db, acc.id):
        raise ConflictError("Нельзя удалить счёт, по которому есть операции")
    if db.scalar(select(exists().where(Planning.funding_account_id == acc.id))):
        raise ConflictError(
            "Счёт указан источником финансирования в планировании — сначала измените план"
        )
    db.delete(acc)
    db.commit()


def close_account(db: Session, user: User, account_id: uuid.UUID) -> Account:
    acc = get_account(db, user, account_id)
    if acc.is_closed:
        raise ConflictError("Счёт уже закрыт")
    balance = account_balance(db, user.id, acc.id)
    if balance != ZERO:
        shown = f"{balance:,.2f}".replace(",", " ").replace(".", ",")
        raise ConflictError(f"Закрыть можно только счёт с нулевым остатком (сейчас {shown} ₽)")
    acc.is_closed = True
    acc.closed_at = datetime.now(UTC)
    db.commit()
    return acc


def reopen_fund(
    db: Session, user: User, account_id: uuid.UUID, on_date: date | None = None
) -> tuple[Account, Account, Decimal]:
    """«Открыть заново» для фонда.

    1. Создаётся новый счёт с типом «Фонд» и тем же наименованием (и параметрами).
    2. На новый счёт переводится остаток старого (операция «Перевод»).
    3. Старый счёт закрывается.
    Возвращает (старый, новый, перенесённая сумма).
    """
    old = get_account(db, user, account_id)
    if old.account_type.code != AccountTypeCode.FUND.value:
        raise ValidationAppError("«Открыть заново» доступно только для счетов с типом «Фонд»")
    if old.is_closed:
        raise ConflictError("Счёт уже закрыт")
    on_date = on_date or date.today()
    balance = account_balance(db, user.id, old.id)

    old.is_closed = True
    old.closed_at = datetime.now(UTC)
    db.flush()  # освобождаем имя для нового открытого счёта
    new = Account(
        owner_id=user.id,
        name=old.name,
        account_type_id=old.account_type_id,
        replenish_period=old.replenish_period,
        replenish_amount=old.replenish_amount,
        months_to_goal=old.months_to_goal,
        target_amount=old.target_amount,
    )
    db.add(new)
    db.flush()

    if balance != ZERO:
        transfer = references.operation_type_by_code(db, OperationTypeCode.TRANSFER.value)
        src, dst = (old, new) if balance > 0 else (new, old)
        db.add(
            Operation(
                owner_id=user.id,
                operation_type_id=transfer.id,
                account_id=src.id,
                target_account_id=dst.id,
                op_date=on_date,
                name="Перенос остатка",
                amount=abs(balance),
                comment="Автоматически при «Открыть заново»",
            )
        )
    _commit(db)
    db.refresh(new)
    return old, new, balance


def replenishment_history(db: Session, user: User, account_id: uuid.UUID) -> Sequence[Operation]:
    """История пополнений: доходы на счёт и входящие переводы."""
    return db.scalars(
        select(Operation)
        .join(OperationType, OperationType.id == Operation.operation_type_id)
        .where(
            Operation.owner_id == user.id,
            or_(
                Operation.target_account_id == account_id,
                (Operation.account_id == account_id)
                & (OperationType.code == OperationTypeCode.INCOME.value),
            ),
        )
        .order_by(Operation.op_date.desc(), Operation.created_at.desc())
    ).all()


# ---------------------------------------------------------------- сводка пополнений

MONTHS_RU = [
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
]


@dataclass(frozen=True)
class ReplenishmentRow:
    start: date  # начало периода
    end: date  # конец периода
    label: str  # «Октябрь 2026» / «IV кв. 2026» / «2026 год»
    amount: Decimal  # пополнено за период (по сегодняшний день)
    target: Decimal | None  # норма пополнения за период (None — регулярность не задана)
    percent: int | None


def _is_reopen_transfer(op: Operation, account: Account) -> bool:
    """Перенос остатка при «Открыть заново» — не пополнение."""
    src = op.account
    return (
        op.target_account_id == account.id
        and src.is_closed
        and src.account_type_id == account.account_type_id
        and src.name.lower() == account.name.lower()
    )


def _month_shift(d: date, delta: int) -> date:
    idx = d.year * 12 + d.month - 1 + delta
    return date(idx // 12, idx % 12 + 1, 1)


ROMAN_QUARTERS = ["I", "II", "III", "IV"]


def _periods(period: ReplenishPeriod, today: date, months: int) -> list[tuple[date, date, str]]:
    """Периоды регулярности, пересекающиеся с последними `months` месяцами (новые сверху).

    Ежеквартально — кварталы, ежегодно — годы; ежемесячно, еженедельно и без
    регулярности — месяцы.
    """
    current = date(today.year, today.month, 1)
    window_start = _month_shift(current, -(months - 1))
    out: list[tuple[date, date, str]] = []
    if period == ReplenishPeriod.QUARTERLY:
        q_start = date(today.year, (today.month - 1) // 3 * 3 + 1, 1)
        while _month_shift(q_start, 3) > window_start:
            q = (q_start.month - 1) // 3
            end = _month_shift(q_start, 3) - timedelta(days=1)
            out.append((q_start, end, f"{ROMAN_QUARTERS[q]} кв. {q_start.year}"))
            q_start = _month_shift(q_start, -3)
    elif period == ReplenishPeriod.YEARLY:
        for year in range(today.year, window_start.year - 1, -1):
            out.append((date(year, 1, 1), date(year, 12, 31), f"{year} год"))
    else:
        for i in range(months):
            m = _month_shift(current, -i)
            end = _month_shift(m, 1) - timedelta(days=1)
            out.append((m, end, f"{MONTHS_RU[m.month - 1]} {m.year}"))
    return out


def replenishment_summary(
    db: Session, user: User, account: Account, today: date | None = None, months: int = 6
) -> list[ReplenishmentRow]:
    """История пополнений за последние `months` месяцев с разбивкой по периодам регулярности.

    Ежемесячно — 6 строк-месяцев; ежеквартально — кварталы, попадающие в эти 6 месяцев
    (в октябре — II, III и IV); ежегодно — годы; еженедельно — 6 месяцев с нормой
    «сумма × дней в месяце / 7». Процент — пополнено за период / норма за период.
    """
    today = today or date.today()
    ops = [
        o
        for o in replenishment_history(db, user, account.id)
        if not _is_reopen_transfer(o, account)
    ]
    period = account.replenish_period
    rows: list[ReplenishmentRow] = []
    for start, end, label in _periods(period, today, months):
        amount = sum((o.amount for o in ops if start <= o.op_date <= end), ZERO)
        target: Decimal | None = None
        if period != ReplenishPeriod.NONE and account.replenish_amount:
            target = account.replenish_amount
            if period == ReplenishPeriod.WEEKLY:
                target = money(target * Decimal(end.day) / Decimal(7))
        percent = int((amount * 100 / target).to_integral_value()) if target else None
        rows.append(ReplenishmentRow(start, end, label, amount, target, percent))
    return rows
