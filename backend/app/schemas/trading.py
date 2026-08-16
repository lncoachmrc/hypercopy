from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field


class PositionOut(BaseModel):
    asset: str
    current_size: Decimal
    target_size: Decimal
    delta: Decimal
    managed: bool
    exchange_verified_at: datetime | None


class ExecutionOut(BaseModel):
    id: str
    asset: str
    state: str
    is_buy: bool
    requested_size: Decimal
    filled_size: Decimal
    avg_price: Decimal | None
    reduce_only: bool
    reject_reason: str | None
    cloid: str
    created_at: datetime


class ClosePositionsIn(BaseModel):
    confirmation: str = Field(pattern='^CLOSE$')
    reason: str = Field(min_length=3, max_length=500)
