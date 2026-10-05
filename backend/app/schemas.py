from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

# Pesos colombianos enteros. strict=True rechaza 100.5, "100" y true: solo enteros JSON.
MAX_AMOUNT = 1_000_000_000
Money = Annotated[int, Field(strict=True, gt=0, le=MAX_AMOUNT)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


class AccountCreate(BaseModel):
    name: Name


class DepositRequest(BaseModel):
    amount: Money
    description: Description | None = None


class TransferRequest(BaseModel):
    from_account_id: UUID
    to_account_id: UUID
    amount: Money
    description: Description | None = None


class PoolCreate(BaseModel):
    name: Name
    goal_amount: Money
    owner_id: UUID
    beneficiary_id: UUID


class ContributionRequest(BaseModel):
    account_id: UUID
    amount: Money


class PoolAction(BaseModel):
    # Sin autenticación real (fuera de alcance): quien pide la acción se declara aquí.
    requested_by: UUID
