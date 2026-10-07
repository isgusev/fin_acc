"""JSON API администратора.

Пользователи (без доступа к их финансам), справочники, налоги, праздники, настройки.
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import schemas
from app.db import get_db
from app.deps import admin_user
from app.models import (
    AccountType,
    ExpenseCategory,
    Holiday,
    IncomeKind,
    OperationType,
    TaxBracket,
    User,
    UserRole,
)
from app.services import references
from app.services import users as users_svc

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(admin_user)])


class AdminUserOut(BaseModel):
    """Только учётные данные — без зарплаты и прочих финансовых полей профиля."""

    model_config = {"from_attributes": True}
    id: uuid.UUID
    email: str
    display_name: str
    role: UserRole
    is_active: bool
    must_change_password: bool


class ActiveIn(BaseModel):
    is_active: bool


class RoleIn(BaseModel):
    role: UserRole


# ---------------------------------------------------------------- пользователи


@router.get("/users", response_model=list[AdminUserOut])
def list_users(db: Session = Depends(get_db)) -> list[User]:
    return list(users_svc.list_users(db))


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: uuid.UUID, db: Session = Depends(get_db), admin: User = Depends(admin_user)
) -> dict[str, str]:
    return {"temporary_password": users_svc.admin_reset_password(db, admin, user_id)}


@router.put("/users/{user_id}/active", response_model=AdminUserOut)
def set_active(
    user_id: uuid.UUID,
    data: ActiveIn,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
) -> User:
    return users_svc.admin_set_active(db, admin, user_id, data.is_active)


@router.put("/users/{user_id}/role", response_model=AdminUserOut)
def set_role(
    user_id: uuid.UUID,
    data: RoleIn,
    db: Session = Depends(get_db),
    admin: User = Depends(admin_user),
) -> User:
    return users_svc.admin_set_role(db, admin, user_id, data.role)


# ---------------------------------------------------------------- настройки


@router.get("/settings")
def get_settings_(db: Session = Depends(get_db)) -> dict[str, bool]:
    return {"registration_open": references.is_registration_open(db)}


@router.put("/settings")
def put_settings(data: schemas.AppSettingsIn, db: Session = Depends(get_db)) -> dict[str, bool]:
    references.update_settings(db, data)
    return {"registration_open": references.is_registration_open(db)}


# ---------------------------------------------------------------- справочники


@router.post("/account-types", response_model=schemas.AccountTypeOut, status_code=201)
def create_account_type(data: schemas.AccountTypeIn, db: Session = Depends(get_db)) -> AccountType:
    return references.create_account_type(db, data)


@router.put("/account-types/{type_id}", response_model=schemas.AccountTypeOut)
def update_account_type(
    type_id: int, data: schemas.AccountTypeIn, db: Session = Depends(get_db)
) -> AccountType:
    return references.update_account_type(db, type_id, data)


@router.delete("/account-types/{type_id}", status_code=204)
def delete_account_type(type_id: int, db: Session = Depends(get_db)) -> None:
    references.delete_account_type(db, type_id)


@router.put("/operation-types/{type_id}", response_model=schemas.OperationTypeOut)
def update_operation_type(
    type_id: int, data: schemas.OperationTypeIn, db: Session = Depends(get_db)
) -> OperationType:
    return references.update_operation_type(db, type_id, data)


@router.get("/income-kinds", response_model=list[schemas.IncomeKindOut])
def list_income_kinds(db: Session = Depends(get_db)) -> list[IncomeKind]:
    return list(references.income_kinds(db))


@router.post("/income-kinds", response_model=schemas.IncomeKindOut, status_code=201)
def create_income_kind(data: schemas.IncomeKindIn, db: Session = Depends(get_db)) -> IncomeKind:
    return references.create_income_kind(db, data)


@router.put("/income-kinds/{kind_id}", response_model=schemas.IncomeKindOut)
def update_income_kind(
    kind_id: int, data: schemas.IncomeKindIn, db: Session = Depends(get_db)
) -> IncomeKind:
    return references.update_income_kind(db, kind_id, data)


@router.delete("/income-kinds/{kind_id}", status_code=204)
def delete_income_kind(kind_id: int, db: Session = Depends(get_db)) -> None:
    references.delete_income_kind(db, kind_id)


@router.get("/expense-categories", response_model=list[schemas.ExpenseCategoryOut])
def list_expense_categories(db: Session = Depends(get_db)) -> list[ExpenseCategory]:
    return list(references.expense_categories(db))


@router.post("/expense-categories", response_model=schemas.ExpenseCategoryOut, status_code=201)
def create_expense_category(
    data: schemas.ExpenseCategoryIn, db: Session = Depends(get_db)
) -> ExpenseCategory:
    return references.create_expense_category(db, data)


@router.put("/expense-categories/{category_id}", response_model=schemas.ExpenseCategoryOut)
def update_expense_category(
    category_id: int, data: schemas.ExpenseCategoryIn, db: Session = Depends(get_db)
) -> ExpenseCategory:
    return references.update_expense_category(db, category_id, data)


@router.delete("/expense-categories/{category_id}", status_code=204)
def delete_expense_category(category_id: int, db: Session = Depends(get_db)) -> None:
    references.delete_expense_category(db, category_id)


# ---------------------------------------------------------------- налоги


@router.get("/tax-brackets", response_model=list[schemas.TaxBracketOut])
def list_tax_brackets(db: Session = Depends(get_db)) -> list[TaxBracket]:
    return list(references.tax_brackets(db))


@router.post("/tax-brackets", response_model=schemas.TaxBracketOut, status_code=201)
def create_tax_bracket(data: schemas.TaxBracketIn, db: Session = Depends(get_db)) -> TaxBracket:
    return references.create_tax_bracket(db, data)


@router.put("/tax-brackets/{bracket_id}", response_model=schemas.TaxBracketOut)
def update_tax_bracket(
    bracket_id: int, data: schemas.TaxBracketIn, db: Session = Depends(get_db)
) -> TaxBracket:
    return references.update_tax_bracket(db, bracket_id, data)


@router.delete("/tax-brackets/{bracket_id}", status_code=204)
def delete_tax_bracket(bracket_id: int, db: Session = Depends(get_db)) -> None:
    references.delete_tax_bracket(db, bracket_id)


# ---------------------------------------------------------------- праздники


@router.get("/holidays", response_model=list[schemas.HolidayOut])
def list_holidays(db: Session = Depends(get_db)) -> list[Holiday]:
    return list(references.holidays(db))


@router.post("/holidays", response_model=schemas.HolidayOut, status_code=201)
def create_holiday(data: schemas.HolidayIn, db: Session = Depends(get_db)) -> Holiday:
    return references.create_holiday(db, data)


@router.put("/holidays/{holiday_id}", response_model=schemas.HolidayOut)
def update_holiday(
    holiday_id: int, data: schemas.HolidayIn, db: Session = Depends(get_db)
) -> Holiday:
    return references.update_holiday(db, holiday_id, data)


@router.delete("/holidays/{holiday_id}", status_code=204)
def delete_holiday(holiday_id: int, db: Session = Depends(get_db)) -> None:
    references.delete_holiday(db, holiday_id)
