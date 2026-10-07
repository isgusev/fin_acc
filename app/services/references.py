"""Справочники, шкала налогов, праздники и общие настройки (изменяет только администратор)."""

from collections.abc import Sequence
from datetime import date

from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import schemas
from app.errors import ConflictError, NotFoundError, ValidationAppError
from app.models import (
    Account,
    AccountType,
    AppSetting,
    ExpenseCategory,
    Holiday,
    IncomeKind,
    Operation,
    OperationType,
    Planning,
    TaxBracket,
)
from app.services.calc import Bracket

# ---------------------------------------------------------------- чтение


def account_types(db: Session) -> Sequence[AccountType]:
    return db.scalars(select(AccountType).order_by(AccountType.sort_order, AccountType.name)).all()


def operation_types(db: Session) -> Sequence[OperationType]:
    return db.scalars(select(OperationType).order_by(OperationType.sort_order)).all()


def income_kinds(db: Session, only_active: bool = False) -> Sequence[IncomeKind]:
    q = select(IncomeKind).order_by(IncomeKind.sort_order, IncomeKind.name)
    if only_active:
        q = q.where(IncomeKind.is_active)
    return db.scalars(q).all()


def account_type_by_code(db: Session, code: str) -> AccountType:
    at = db.scalar(select(AccountType).where(AccountType.code == code))
    if at is None:
        raise NotFoundError(f"Не найден системный тип счёта «{code}»")
    return at


def operation_type_by_code(db: Session, code: str) -> OperationType:
    ot = db.scalar(select(OperationType).where(OperationType.code == code))
    if ot is None:
        raise NotFoundError(f"Не найден тип операции «{code}»")
    return ot


def income_kind_by_code(db: Session, code: str) -> IncomeKind:
    ik = db.scalar(select(IncomeKind).where(IncomeKind.code == code))
    if ik is None:
        raise NotFoundError(f"Не найден вид дохода «{code}»")
    return ik


def get_account_type(db: Session, type_id: int) -> AccountType:
    at = db.get(AccountType, type_id)
    if at is None:
        raise NotFoundError("Тип счёта не найден", "account_type_id")
    return at


def get_income_kind(db: Session, kind_id: int) -> IncomeKind:
    ik = db.get(IncomeKind, kind_id)
    if ik is None:
        raise NotFoundError("Вид дохода не найден", "income_kind_id")
    return ik


def tax_brackets(db: Session) -> Sequence[TaxBracket]:
    return db.scalars(select(TaxBracket).order_by(TaxBracket.income_from)).all()


def tax_scale(db: Session) -> list[Bracket]:
    return [Bracket(b.income_from, b.income_to, b.rate) for b in tax_brackets(db)]


def holidays(db: Session, from_year: int | None = None) -> Sequence[Holiday]:
    q = select(Holiday).order_by(Holiday.date_from)
    if from_year is not None:
        q = q.where(Holiday.date_to >= date(from_year, 1, 1))
    return db.scalars(q).all()


def holiday_ranges_for_year(db: Session, year: int) -> list[tuple[date, date]]:
    rows = db.execute(
        select(Holiday.date_from, Holiday.date_to).where(
            Holiday.date_to >= date(year, 1, 1), Holiday.date_from <= date(year, 12, 31)
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


# ---------------------------------------------------------------- настройки

REGISTRATION_OPEN = "registration_open"


def get_setting(db: Session, key: str, default: str) -> str:
    s = db.get(AppSetting, key)
    return s.value if s else default


def is_registration_open(db: Session) -> bool:
    return get_setting(db, REGISTRATION_OPEN, "true") == "true"


def update_settings(db: Session, data: schemas.AppSettingsIn) -> None:
    value = "true" if data.registration_open else "false"
    s = db.get(AppSetting, REGISTRATION_OPEN)
    if s is None:
        db.add(AppSetting(key=REGISTRATION_OPEN, value=value))
    else:
        s.value = value
    db.commit()


# ---------------------------------------------------------------- изменение справочников


def _commit_unique(db: Session, field: str = "name") -> None:
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise ConflictError("Запись с таким наименованием уже существует", field) from e


def create_account_type(db: Session, data: schemas.AccountTypeIn) -> AccountType:
    at = AccountType(**data.model_dump())
    db.add(at)
    _commit_unique(db)
    return at


def update_account_type(db: Session, type_id: int, data: schemas.AccountTypeIn) -> AccountType:
    at = get_account_type(db, type_id)
    if at.code is not None and data.single_per_user != at.single_per_user:
        raise ValidationAppError(
            "У системного типа счёта нельзя менять ограничение «не больше одного счёта»",
            "single_per_user",
        )
    if data.single_per_user and not at.single_per_user:
        dup = db.execute(
            select(Account.owner_id)
            .where(Account.account_type_id == at.id, ~Account.is_closed)
            .group_by(Account.owner_id)
            .having(func.count() > 1)
            .limit(1)
        ).first()
        if dup:
            raise ConflictError(
                "У некоторых пользователей уже больше одного открытого счёта этого типа",
                "single_per_user",
            )
    for k, v in data.model_dump().items():
        setattr(at, k, v)
    _commit_unique(db)
    return at


def delete_account_type(db: Session, type_id: int) -> None:
    at = get_account_type(db, type_id)
    if at.code is not None:
        raise ConflictError("Системный тип счёта удалить нельзя")
    if db.scalar(select(exists().where(Account.account_type_id == at.id))):
        raise ConflictError("Тип счёта используется в счетах — удаление невозможно")
    db.delete(at)
    db.commit()


def update_operation_type(
    db: Session, type_id: int, data: schemas.OperationTypeIn
) -> OperationType:
    ot = db.get(OperationType, type_id)
    if ot is None:
        raise NotFoundError("Тип операции не найден")
    ot.name = data.name
    _commit_unique(db)
    return ot


def create_income_kind(db: Session, data: schemas.IncomeKindIn) -> IncomeKind:
    ik = IncomeKind(**data.model_dump())
    db.add(ik)
    _commit_unique(db)
    return ik


def update_income_kind(db: Session, kind_id: int, data: schemas.IncomeKindIn) -> IncomeKind:
    ik = get_income_kind(db, kind_id)
    if ik.code is not None and not data.is_active:
        raise ValidationAppError("Системный вид дохода нельзя отключить", "is_active")
    for k, v in data.model_dump().items():
        setattr(ik, k, v)
    _commit_unique(db)
    return ik


def delete_income_kind(db: Session, kind_id: int) -> None:
    ik = get_income_kind(db, kind_id)
    if ik.code is not None:
        raise ConflictError("Системный вид дохода удалить нельзя")
    if db.scalar(select(exists().where(Planning.income_kind_id == ik.id))):
        raise ConflictError("Вид дохода используется в планировании — удаление невозможно")
    db.delete(ik)
    db.commit()


# ---------------------------------------------------------------- налоги и праздники


def _check_bracket_overlap(db: Session, data: schemas.TaxBracketIn, exclude_id: int | None) -> None:
    for b in tax_brackets(db):
        if b.id == exclude_id:
            continue
        a_hi = data.income_to
        b_hi = b.income_to
        # интервалы [from, to) пересекаются?
        if (a_hi is None or b.income_from < a_hi) and (b_hi is None or data.income_from < b_hi):
            raise ConflictError(
                "Диапазон пересекается с уже существующей ступенью шкалы", "income_from"
            )


def get_tax_bracket(db: Session, bracket_id: int) -> TaxBracket:
    b = db.get(TaxBracket, bracket_id)
    if b is None:
        raise NotFoundError("Ступень шкалы налогов не найдена")
    return b


def create_tax_bracket(db: Session, data: schemas.TaxBracketIn) -> TaxBracket:
    _check_bracket_overlap(db, data, None)
    b = TaxBracket(**data.model_dump())
    db.add(b)
    db.commit()
    return b


def update_tax_bracket(db: Session, bracket_id: int, data: schemas.TaxBracketIn) -> TaxBracket:
    b = get_tax_bracket(db, bracket_id)
    _check_bracket_overlap(db, data, b.id)
    for k, v in data.model_dump().items():
        setattr(b, k, v)
    db.commit()
    return b


def delete_tax_bracket(db: Session, bracket_id: int) -> None:
    db.delete(get_tax_bracket(db, bracket_id))
    db.commit()


def get_holiday(db: Session, holiday_id: int) -> Holiday:
    h = db.get(Holiday, holiday_id)
    if h is None:
        raise NotFoundError("Праздничная дата не найдена")
    return h


def create_holiday(db: Session, data: schemas.HolidayIn) -> Holiday:
    h = Holiday(**data.model_dump())
    db.add(h)
    db.commit()
    return h


def update_holiday(db: Session, holiday_id: int, data: schemas.HolidayIn) -> Holiday:
    h = get_holiday(db, holiday_id)
    for k, v in data.model_dump().items():
        setattr(h, k, v)
    db.commit()
    return h


def delete_holiday(db: Session, holiday_id: int) -> None:
    db.delete(get_holiday(db, holiday_id))
    db.commit()


# ---------------------------------------------------------------- категории расходов


def expense_categories(db: Session, only_active: bool = False) -> Sequence[ExpenseCategory]:
    q = select(ExpenseCategory).order_by(ExpenseCategory.sort_order, ExpenseCategory.name)
    if only_active:
        q = q.where(ExpenseCategory.is_active)
    return db.scalars(q).all()


def get_expense_category(db: Session, category_id: int) -> ExpenseCategory:
    c = db.get(ExpenseCategory, category_id)
    if c is None:
        raise NotFoundError("Категория не найдена", "category_id")
    return c


def create_expense_category(db: Session, data: schemas.ExpenseCategoryIn) -> ExpenseCategory:
    c = ExpenseCategory(**data.model_dump())
    db.add(c)
    _commit_unique(db)
    return c


def update_expense_category(
    db: Session, category_id: int, data: schemas.ExpenseCategoryIn
) -> ExpenseCategory:
    c = get_expense_category(db, category_id)
    for k, v in data.model_dump().items():
        setattr(c, k, v)
    _commit_unique(db)
    return c


def delete_expense_category(db: Session, category_id: int) -> None:
    c = get_expense_category(db, category_id)
    if db.scalar(select(exists().where(Operation.category_id == c.id))):
        raise ConflictError(
            "Категория используется в операциях — удаление невозможно (её можно отключить)"
        )
    db.delete(c)
    db.commit()
