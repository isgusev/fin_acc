"""JSON API: счета, операции, планирование, сводка. Всё — только данные текущего пользователя."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app import schemas
from app.db import get_db
from app.deps import current_user
from app.models import AccountTypeCode, User
from app.services import accounts as accounts_svc
from app.services import balances, references
from app.services import operations as ops_svc
from app.services import planning as plan_svc

router = APIRouter(tags=["finance"])


# ---------------------------------------------------------------- справочники (чтение)


@router.get("/references")
def get_references(
    db: Session = Depends(get_db), _: User = Depends(current_user)
) -> dict[str, object]:
    return {
        "account_types": [
            schemas.AccountTypeOut.model_validate(x) for x in references.account_types(db)
        ],
        "operation_types": [
            schemas.OperationTypeOut.model_validate(x) for x in references.operation_types(db)
        ],
        "income_kinds": [
            schemas.IncomeKindOut.model_validate(x) for x in references.income_kinds(db, True)
        ],
    }


# ---------------------------------------------------------------- сводка


@router.get("/summary/balances", response_model=list[schemas.TypeBalanceOut])
def summary_balances(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> list[schemas.TypeBalanceOut]:
    return [
        schemas.TypeBalanceOut(account_type_id=t.id, account_type_name=t.name, balance=b)
        for t, b in balances.balances_by_type(db, user.id)
    ]


# ---------------------------------------------------------------- счета


@router.get("/accounts", response_model=list[schemas.AccountOut])
def list_accounts(
    type_code: AccountTypeCode | None = None,
    include_closed: bool = True,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[schemas.AccountOut]:
    accs = accounts_svc.list_accounts(
        db, user, type_code=type_code.value if type_code else None, include_closed=include_closed
    )
    return accounts_svc.accounts_with_balances(db, user, accs)


@router.post("/accounts", response_model=schemas.AccountOut, status_code=201)
def create_account(
    data: schemas.AccountIn, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> schemas.AccountOut:
    acc = accounts_svc.create_account(db, user, data)
    return accounts_svc.to_out(acc, balances.ZERO)


@router.get("/accounts/{account_id}", response_model=schemas.AccountOut)
def get_account(
    account_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> schemas.AccountOut:
    acc = accounts_svc.get_account(db, user, account_id)
    return accounts_svc.to_out(acc, balances.account_balance(db, user.id, acc.id))


@router.put("/accounts/{account_id}", response_model=schemas.AccountOut)
def update_account(
    account_id: uuid.UUID,
    data: schemas.AccountIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> schemas.AccountOut:
    acc = accounts_svc.update_account(db, user, account_id, data)
    return accounts_svc.to_out(acc, balances.account_balance(db, user.id, acc.id))


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(
    account_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> None:
    accounts_svc.delete_account(db, user, account_id)


@router.post("/accounts/{account_id}/close", response_model=schemas.AccountOut)
def close_account(
    account_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> schemas.AccountOut:
    acc = accounts_svc.close_account(db, user, account_id)
    return accounts_svc.to_out(acc, balances.ZERO)


@router.post("/accounts/{account_id}/reopen", response_model=schemas.FundReopenOut)
def reopen_fund(
    account_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> schemas.FundReopenOut:
    old, new, moved = accounts_svc.reopen_fund(db, user, account_id)
    return schemas.FundReopenOut(
        old_account_id=old.id,
        new_account=accounts_svc.to_out(new, balances.account_balance(db, user.id, new.id)),
        transferred=moved,
    )


@router.get("/accounts/{account_id}/replenishments", response_model=list[schemas.OperationOut])
def replenishments(
    account_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> list[schemas.OperationOut]:
    accounts_svc.get_account(db, user, account_id)
    return [ops_svc.to_out(o) for o in accounts_svc.replenishment_history(db, user, account_id)]


# ---------------------------------------------------------------- операции


@router.get("/operations")
def list_operations(
    f: Annotated[schemas.OperationFilter, Query()],
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict[str, object]:
    rows, total = ops_svc.list_operations(db, user, f)
    return {"items": [ops_svc.to_out(o) for o in rows], "total": total}


@router.post("/operations", response_model=schemas.OperationOut, status_code=201)
def create_operation(
    data: schemas.OperationIn, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> schemas.OperationOut:
    return ops_svc.to_out(ops_svc.create_operation(db, user, data))


@router.get("/operations/{op_id}", response_model=schemas.OperationOut)
def get_operation(
    op_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> schemas.OperationOut:
    return ops_svc.to_out(ops_svc.get_operation(db, user, op_id))


@router.put("/operations/{op_id}", response_model=schemas.OperationOut)
def update_operation(
    op_id: uuid.UUID,
    data: schemas.OperationIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> schemas.OperationOut:
    return ops_svc.to_out(ops_svc.update_operation(db, user, op_id, data))


@router.delete("/operations/{op_id}", status_code=204)
def delete_operation(
    op_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> None:
    ops_svc.delete_operation(db, user, op_id)


# ---------------------------------------------------------------- планирование


@router.get("/planning", response_model=list[schemas.PlanningOut])
def list_plans(
    year: int | None = Query(default=None, ge=2000, le=2100),
    operation_type: str | None = Query(default=None, pattern="^(income|expense)$"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[schemas.PlanningOut]:
    year = year or date.today().year
    return plan_svc.plans_out(
        db, plan_svc.list_plans(db, user, year=year, operation_type=operation_type)
    )


@router.post("/planning", response_model=schemas.PlanningOut, status_code=201)
def create_plan(
    data: schemas.PlanningIn, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> schemas.PlanningOut:
    return plan_svc.to_out(plan_svc.create_plan(db, user, data))


@router.get("/planning/{plan_id}", response_model=schemas.PlanningOut)
def get_plan(
    plan_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> schemas.PlanningOut:
    p = plan_svc.get_plan(db, user, plan_id)
    return plan_svc.plans_out(db, [p])[0]


@router.put("/planning/{plan_id}", response_model=schemas.PlanningOut)
def update_plan(
    plan_id: uuid.UUID,
    data: schemas.PlanningIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> schemas.PlanningOut:
    p = plan_svc.update_plan(db, user, plan_id, data)
    return plan_svc.plans_out(db, [p])[0]


@router.delete("/planning/{plan_id}", status_code=204)
def delete_plan(
    plan_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> None:
    plan_svc.delete_plan(db, user, plan_id)


@router.post("/planning/salary", response_model=schemas.SalaryCalcOut)
def calculate_salary(
    data: schemas.SalaryCalcIn, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> schemas.SalaryCalcOut:
    r = plan_svc.calculate_salary(db, user, data.start_date, data.replace)
    return schemas.SalaryCalcOut(
        created=len(r.created),
        replaced=r.replaced,
        skipped=r.skipped,
        plans=plan_svc.plans_out(db, r.created),
    )
