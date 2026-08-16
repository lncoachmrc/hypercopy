from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field


class UserOut(BaseModel):
    id: str
    auth_wallet: str
    role: str
    state: str
    copy_state: str
    manual_trade_policy: str
    email: str | None = None
    display_name: str | None = None
    shadow_started_at: datetime | None = None
    trading_account: dict | None = None
    risk_state: str = 'NORMAL'


class TradingAccountIn(BaseModel):
    account_address: str
    agent_private_key: str = Field(min_length=32, max_length=100)


class RiskProfileIn(BaseModel):
    multiplier: Decimal = Field(default=Decimal('1'), gt=0, le=10)
    max_notional_per_trade: Decimal = Field(default=Decimal('1000'), gt=0)
    max_total_exposure: Decimal = Field(default=Decimal('5000'), gt=0)
    max_asset_exposure: Decimal = Field(default=Decimal('2500'), gt=0)
    max_leverage: Decimal = Field(default=Decimal('3'), gt=0, le=50)
    max_positions: int = Field(default=5, ge=1, le=100)
    max_drawdown_pct: Decimal = Field(default=Decimal('20'), gt=0, le=100)
    max_daily_loss_pct: Decimal = Field(default=Decimal('10'), gt=0, le=100)
    min_notional: Decimal = Field(default=Decimal('10'), ge=10)
    max_slippage_bps: int = Field(default=50, ge=1, le=500)
    close_only: bool = False
    allow_assets: list[str] = Field(default_factory=list)
    block_assets: list[str] = Field(default_factory=list)
