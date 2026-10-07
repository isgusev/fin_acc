"""Операции со счетами: доход, расход, перевод."""

import uuid
from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from app import schemas
from app.errors import (
    AppError,
    ConflictError,
    NotFoundError,
    RowsValidationError,
    ValidationAppError,
)
from app.models import (
    Account,
    AccountTypeCode,
    Operation,
    OperationType,
    OperationTypeCode,
    Planning,
    User,
)
from app.services import accounts as accounts_svc
from app.services import references


def get_operation(db: Session, user: User, op_id: uuid.UUID) -> Operation:
    op = db.get(Operation, op_id)
    if op is None or op.owner_id != user.id:
        raise NotFoundError("Операция не найдена")
    return op


def _get_plan(db: Session, user: User, plan_id: uuid.UUID) -> Planning:
    plan = db.get(Planning, plan_id)
    if plan is None or plan.owner_id != user.id:
        raise NotFoundError("Запись планирования не найдена", "plan_id")
    return plan


def _open_account(db: Session, user: User, account_id: uuid.UUID, field: str) -> Account:
    acc = accounts_svc.get_account(db, user, account_id, field)
    if acc.is_closed:
        raise ConflictError(f"Счёт «{acc.name}» закрыт — операции по нему невозможны", field)
    return acc


def _resolve_category(
    db: Session, data: schemas.OperationIn, account: Account, current_category_id: int | None
) -> int | None:
    """Категория — только у расходов; для расходов со счёта «Текущий» обязательна."""
    if data.operation_type != OperationTypeCode.EXPENSE:
        if data.category_id is not None:
            raise ValidationAppError("Категория указывается только для расходов", "category_id")
        return None
    if data.category_id is None:
        if account.account_type.code == AccountTypeCode.CURRENT.value:
            raise ValidationAppError(
                "Для расхода со счёта с типом «Текущий» укажите категорию", "category_id"
            )
        return None
    category = references.get_expense_category(db, data.category_id)
    # отключённую категорию можно оставить у старой операции, но не выбрать заново
    if not category.is_active and category.id != current_category_id:
        raise ValidationAppError("Эта категория отключена администратором", "category_id")
    return category.id


def _resolve(
    db: Session, user: User, data: schemas.OperationIn, current_category_id: int | None = None
) -> dict[str, object]:
    """Проверяет данные операции по правилам её типа и возвращает поля для записи в БД."""
    ot = references.operation_type_by_code(db, data.operation_type)
    target_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None

    if data.operation_type == OperationTypeCode.INCOME:
        account = accounts_svc.get_unallocated_account(db, user)
        if data.account_id is not None and data.account_id != account.id:
            raise ValidationAppError(
                "Доход зачисляется только на счёт с типом «Нераспределённый доход»", "account_id"
            )
        if data.target_account_id is not None:
            raise ValidationAppError("У дохода не бывает счёта-назначения", "target_account_id")
        if data.plan_id is None:
            raise ValidationAppError(
                "Для дохода обязательно укажите запись планирования", "plan_id"
            )
        plan = _get_plan(db, user, data.plan_id)
        if plan.operation_type.code != OperationTypeCode.INCOME.value:
            raise ValidationAppError(
                "Доход можно связать только с планом с типом «Доход»", "plan_id"
            )
        plan_id = plan.id

    elif data.operation_type == OperationTypeCode.EXPENSE:
        if data.account_id is None:
            raise ValidationAppError("Укажите счёт", "account_id")
        account = _open_account(db, user, data.account_id, "account_id")
        if not data.name:
            raise ValidationAppError("Для расхода обязательно укажите наименование", "name")
        if data.target_account_id is not None:
            raise ValidationAppError("У расхода не бывает счёта-назначения", "target_account_id")
        if data.plan_id is not None:
            plan = _get_plan(db, user, data.plan_id)
            if plan.operation_type.code != OperationTypeCode.EXPENSE.value:
                raise ValidationAppError(
                    "Расход можно связать только с планом с типом «Расход»", "plan_id"
                )
            plan_id = plan.id

    else:  # transfer
        if data.account_id is None:
            raise ValidationAppError("Укажите счёт-источник", "account_id")
        if data.target_account_id is None:
            raise ValidationAppError("Укажите счёт-назначение", "target_account_id")
        if data.account_id == data.target_account_id:
            raise ValidationAppError(
                "Счёт-источник и счёт-назначение должны различаться", "target_account_id"
            )
        account = _open_account(db, user, data.account_id, "account_id")
        target = _open_account(db, user, data.target_account_id, "target_account_id")
        target_id = target.id
        if data.plan_id is not None:
            raise ValidationAppError("Перевод нельзя связать с планом", "plan_id")

    category_id = _resolve_category(db, data, account, current_category_id)
    return {
        "operation_type_id": ot.id,
        "account_id": account.id,
        "target_account_id": target_id,
        "op_date": data.op_date,
        "name": data.name,
        "amount": data.amount,
        "category_id": category_id,
        "comment": data.comment,
        "plan_id": plan_id,
    }


def _ensure_not_touching_closed(op: Operation) -> None:
    for acc in (op.account, op.target_account):
        if acc is not None and acc.is_closed:
            raise ConflictError(
                f"Операция относится к закрытому счёту «{acc.name}» — изменить её нельзя"
            )


def create_operation(db: Session, user: User, data: schemas.OperationIn) -> Operation:
    op = Operation(owner_id=user.id, **_resolve(db, user, data))
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def create_expenses_bulk(
    db: Session,
    user: User,
    items: Sequence[tuple[schemas.OperationIn, str | None, str | None]],
) -> list[Operation]:
    """Создаёт расходы пачкой (импорт выписки) — всё или ничего.

    items: (данные операции, описание из банка, номер карты). Ошибки собираются по всем
    строкам сразу и выбрасываются одним RowsValidationError.
    """
    errors: dict[int, list[tuple[str | None, str]]] = {}
    ops: list[Operation] = []
    for i, (data, bank_description, bank_card) in enumerate(items):
        if data.operation_type != OperationTypeCode.EXPENSE:
            errors[i] = [("operation_type", "Импортируются только расходы")]
            continue
        try:
            fields = _resolve(db, user, data)
        except AppError as e:
            errors[i] = [(e.field, e.message)]
            continue
        ops.append(
            Operation(
                owner_id=user.id,
                bank_description=bank_description,
                bank_card=bank_card,
                **fields,
            )
        )
    if errors:
        raise RowsValidationError(errors)
    db.add_all(ops)
    db.commit()
    return ops


def update_operation(
    db: Session, user: User, op_id: uuid.UUID, data: schemas.OperationIn
) -> Operation:
    op = get_operation(db, user, op_id)
    _ensure_not_touching_closed(op)
    for k, v in _resolve(db, user, data, op.category_id).items():
        setattr(op, k, v)
    db.commit()
    db.refresh(op)
    return op


def delete_operation(db: Session, user: User, op_id: uuid.UUID) -> None:
    op = get_operation(db, user, op_id)
    _ensure_not_touching_closed(op)
    db.delete(op)
    db.commit()


def list_operations(
    db: Session, user: User, f: schemas.OperationFilter
) -> tuple[Sequence[Operation], int]:
    src = aliased(Account)
    dst = aliased(Account)
    q = (
        select(Operation)
        .join(src, src.id == Operation.account_id)
        .outerjoin(dst, dst.id == Operation.target_account_id)
        .join(OperationType, OperationType.id == Operation.operation_type_id)
        .where(Operation.owner_id == user.id)
    )
    if f.date_from:
        q = q.where(Operation.op_date >= f.date_from)
    if f.date_to:
        q = q.where(Operation.op_date <= f.date_to)
    if f.account_type_id is not None:
        q = q.where(
            or_(src.account_type_id == f.account_type_id, dst.account_type_id == f.account_type_id)
        )
    if f.account_id is not None:
        q = q.where(
            or_(Operation.account_id == f.account_id, Operation.target_account_id == f.account_id)
        )
    if f.operation_type is not None:
        q = q.where(OperationType.code == f.operation_type)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(
        q.order_by(Operation.op_date.desc(), Operation.created_at.desc())
        .limit(f.limit)
        .offset(f.offset)
    ).all()
    return rows, total


def to_out(op: Operation) -> schemas.OperationOut:
    return schemas.OperationOut(
        id=op.id,
        operation_type=schemas.OperationTypeOut.model_validate(op.operation_type),
        account_id=op.account_id,
        account_name=op.account.name,
        target_account_id=op.target_account_id,
        target_account_name=op.target_account.name if op.target_account else None,
        op_date=op.op_date,
        name=op.name,
        amount=op.amount,
        category_id=op.category_id,
        category_name=op.category.name if op.category else None,
        bank_description=op.bank_description,
        comment=op.comment,
        plan_id=op.plan_id,
        created_at=op.created_at,
        updated_at=op.updated_at,
    )
