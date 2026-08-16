from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

D = Numeric(30, 12)


class Role(str, enum.Enum):
    USER = 'USER'
    ADMIN = 'ADMIN'
    SUPERADMIN = 'SUPERADMIN'


class UserState(str, enum.Enum):
    ACTIVE = 'ACTIVE'
    SUSPENDED = 'SUSPENDED'


class CopyState(str, enum.Enum):
    SHADOW = 'SHADOW'
    ACTIVE = 'ACTIVE'
    PAUSED = 'PAUSED'


class ManualTradePolicy(str, enum.Enum):
    STRICT = 'STRICT'
    COEXIST = 'COEXIST'
    MANUAL_WINS = 'MANUAL_WINS'


class CredentialStatus(str, enum.Enum):
    ACTIVE = 'ACTIVE'
    EXPIRING = 'EXPIRING'
    EXPIRED = 'EXPIRED'
    REVOKED = 'REVOKED'
    DISABLED = 'DISABLED'


class RiskHalt(str, enum.Enum):
    NORMAL = 'NORMAL'
    DRAWDOWN_HALT = 'DRAWDOWN_HALT'
    DAILY_LOSS_HALT = 'DAILY_LOSS_HALT'
    CREDENTIAL_EXPIRED = 'CREDENTIAL_EXPIRED'


class JobState(str, enum.Enum):
    QUEUED = 'QUEUED'
    PROCESSING = 'PROCESSING'
    RETRYING = 'RETRYING'
    DONE = 'DONE'
    SKIPPED = 'SKIPPED'
    DEAD = 'DEAD'


class ExecutionState(str, enum.Enum):
    SUBMITTING = 'SUBMITTING'
    UNKNOWN = 'UNKNOWN'
    FILLED = 'FILLED'
    REJECTED = 'REJECTED'
    CANCELED = 'CANCELED'


class BaseUuid:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class User(BaseUuid, Timestamped, Base):
    __tablename__ = 'users'
    auth_wallet: Mapped[str] = mapped_column(String(42), unique=True, index=True, nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role, name='role_enum', native_enum=False, length=32), default=Role.USER, index=True, nullable=False)
    state: Mapped[UserState] = mapped_column(Enum(UserState, name='user_state_enum', native_enum=False, length=32), default=UserState.ACTIVE, index=True, nullable=False)
    copy_state: Mapped[CopyState] = mapped_column(Enum(CopyState, name='copy_state_enum', native_enum=False, length=32), default=CopyState.SHADOW, nullable=False)
    manual_trade_policy: Mapped[ManualTradePolicy] = mapped_column(Enum(ManualTradePolicy, name='manual_policy_enum', native_enum=False, length=32), default=ManualTradePolicy.COEXIST, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(80))
    email: Mapped[str | None] = mapped_column(String(255))
    shadow_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trading_account: Mapped['TradingAccount | None'] = relationship(back_populates='user', uselist=False, cascade='all, delete-orphan')


class AuthNonce(BaseUuid, Base):
    __tablename__ = 'auth_nonces'
    nonce: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    address: Mapped[str] = mapped_column(String(42), index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TradingAccount(BaseUuid, Timestamped, Base):
    __tablename__ = 'trading_accounts'
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), unique=True, index=True)
    account_address: Mapped[str] = mapped_column(String(42), index=True, nullable=False)
    agent_address: Mapped[str] = mapped_column(String(42), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(64), default='hypercopy', nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    user: Mapped[User] = relationship(back_populates='trading_account')
    credential: Mapped['SigningCredential | None'] = relationship(back_populates='trading_account', uselist=False, cascade='all, delete-orphan')


class SigningCredential(BaseUuid, Timestamped, Base):
    __tablename__ = 'signing_credentials'
    trading_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('trading_accounts.id', ondelete='CASCADE'), unique=True)
    ciphertext_b64: Mapped[str] = mapped_column(Text, nullable=False)
    nonce_b64: Mapped[str] = mapped_column(String(64), nullable=False)
    wrapped_dek_b64: Mapped[str] = mapped_column(Text, nullable=False)
    wrap_nonce_b64: Mapped[str | None] = mapped_column(String(64))
    key_provider: Mapped[str] = mapped_column(String(24), nullable=False)
    key_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    agent_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[CredentialStatus] = mapped_column(Enum(CredentialStatus, name='credential_status_enum', native_enum=False, length=32), default=CredentialStatus.ACTIVE, index=True)
    trading_account: Mapped[TradingAccount] = relationship(back_populates='credential')


class RiskProfile(BaseUuid, Timestamped, Base):
    __tablename__ = 'risk_profiles'
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), unique=True)
    multiplier: Mapped[Decimal] = mapped_column(D, default=Decimal('1'))
    max_notional_per_trade: Mapped[Decimal] = mapped_column(D, default=Decimal('1000'))
    max_total_exposure: Mapped[Decimal] = mapped_column(D, default=Decimal('5000'))
    max_asset_exposure: Mapped[Decimal] = mapped_column(D, default=Decimal('2500'))
    max_leverage: Mapped[Decimal] = mapped_column(D, default=Decimal('3'))
    max_positions: Mapped[int] = mapped_column(Integer, default=5)
    max_drawdown_pct: Mapped[Decimal] = mapped_column(D, default=Decimal('20'))
    max_daily_loss_pct: Mapped[Decimal] = mapped_column(D, default=Decimal('10'))
    min_notional: Mapped[Decimal] = mapped_column(D, default=Decimal('10'))
    max_slippage_bps: Mapped[int] = mapped_column(Integer, default=50)
    close_only: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_assets: Mapped[list] = mapped_column(JSON, default=list)
    block_assets: Mapped[list] = mapped_column(JSON, default=list)


class RiskState(BaseUuid, Timestamped, Base):
    __tablename__ = 'risk_state'
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), unique=True)
    state: Mapped[RiskHalt] = mapped_column(Enum(RiskHalt, name='risk_halt_enum', native_enum=False, length=32), default=RiskHalt.NORMAL)
    peak_equity: Mapped[Decimal | None] = mapped_column(D)
    day_start_equity: Mapped[Decimal | None] = mapped_column(D)
    day_key: Mapped[str | None] = mapped_column(String(10))
    near_liquidation: Mapped[bool] = mapped_column(Boolean, default=False)
    liquidation_distance_pct: Mapped[Decimal | None] = mapped_column(D)
    reason: Mapped[str | None] = mapped_column(Text)


class Plan(Base, Timestamped):
    __tablename__ = 'plans'
    slug: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    limits: Mapped[dict] = mapped_column(JSON, default=dict)


class Subscription(BaseUuid, Timestamped, Base):
    __tablename__ = 'subscriptions'
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), unique=True, index=True)
    plan_slug: Mapped[str] = mapped_column(ForeignKey('plans.slug'), default='trial')
    stripe_customer_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32), default='trialing', index=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trial_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StripeEvent(BaseUuid, Base):
    __tablename__ = 'stripe_events'
    event_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class MasterEvent(BaseUuid, Base):
    __tablename__ = 'master_events'
    exchange_event_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    asset: Mapped[str] = mapped_column(String(24), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    size: Mapped[Decimal] = mapped_column(D, nullable=False)
    price: Mapped[Decimal] = mapped_column(D, nullable=False)
    start_position: Mapped[Decimal] = mapped_column(D, default=Decimal(0))
    position_after: Mapped[Decimal] = mapped_column(D, nullable=False)
    master_equity: Mapped[Decimal] = mapped_column(D, nullable=False)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (Index('ix_master_asset_ts', 'asset', 'event_ts'),)


class CopyJob(BaseUuid, Base):
    __tablename__ = 'copy_jobs'
    master_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('master_events.id', ondelete='SET NULL'))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    asset: Mapped[str] = mapped_column(String(24), nullable=False)
    origin: Mapped[str] = mapped_column(String(24), default='EVENT')
    state: Mapped[JobState] = mapped_column(Enum(JobState, name='job_state_enum', native_enum=False, length=32), default=JobState.QUEUED, index=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    owner: Mapped[str | None] = mapped_column(String(80), index=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    __table_args__ = (
        UniqueConstraint('master_event_id', 'user_id', name='uq_job_master_user'),
        Index('ix_jobs_state_created', 'state', 'created_at'),
    )


class Execution(BaseUuid, Base):
    __tablename__ = 'executions'
    copy_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('copy_jobs.id', ondelete='CASCADE'))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    attempt_kind: Mapped[str] = mapped_column(String(1), default='o')
    cloid: Mapped[str] = mapped_column(String(34), unique=True, nullable=False)
    state: Mapped[ExecutionState] = mapped_column(Enum(ExecutionState, name='execution_state_enum', native_enum=False, length=32), default=ExecutionState.SUBMITTING, index=True)
    asset: Mapped[str] = mapped_column(String(24), nullable=False)
    is_buy: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requested_size: Mapped[Decimal] = mapped_column(D, nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    limit_px: Mapped[Decimal] = mapped_column(D, nullable=False)
    exchange_oid: Mapped[str | None] = mapped_column(String(64))
    filled_size: Mapped[Decimal] = mapped_column(D, default=Decimal(0))
    avg_price: Mapped[Decimal | None] = mapped_column(D)
    reject_reason: Mapped[str | None] = mapped_column(Text)
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('copy_job_id', 'attempt_kind', name='uq_execution_job_kind'),)


class Fill(BaseUuid, Base):
    __tablename__ = 'fills'
    exchange_fill_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('executions.id', ondelete='SET NULL'), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    asset: Mapped[str] = mapped_column(String(24), nullable=False)
    size: Mapped[Decimal] = mapped_column(D, nullable=False)
    price: Mapped[Decimal] = mapped_column(D, nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    closed_pnl: Mapped[Decimal | None] = mapped_column(D)
    fee: Mapped[Decimal | None] = mapped_column(D)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class PositionLedger(BaseUuid, Base):
    __tablename__ = 'position_ledger'
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    asset: Mapped[str] = mapped_column(String(24), nullable=False)
    size: Mapped[Decimal] = mapped_column(D, default=Decimal(0))
    target_size: Mapped[Decimal] = mapped_column(D, default=Decimal(0))
    mark_price: Mapped[Decimal] = mapped_column(D, default=Decimal(0))
    entry_notional: Mapped[Decimal] = mapped_column(D, default=Decimal(0))
    managed: Mapped[bool] = mapped_column(Boolean, default=True)
    last_execution_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('executions.id', ondelete='SET NULL'))
    exchange_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), index=True)
    __table_args__ = (UniqueConstraint('user_id', 'asset', name='uq_ledger_user_asset'),)


class EquitySnapshot(BaseUuid, Base):
    __tablename__ = 'equity_snapshots'
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    account_value: Mapped[Decimal] = mapped_column(D, nullable=False)
    free_margin: Mapped[Decimal] = mapped_column(D, default=Decimal(0))
    unmanaged_margin: Mapped[Decimal] = mapped_column(D, default=Decimal(0))
    __table_args__ = (UniqueConstraint('user_id', 'taken_at', name='uq_equity_user_taken'), Index('ix_equity_user_taken', 'user_id', 'taken_at'))


class ReconciliationRun(BaseUuid, Base):
    __tablename__ = 'reconciliation_runs'
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default='RUNNING')
    discrepancy_type: Mapped[str | None] = mapped_column(String(64), index=True)
    drift_pct: Mapped[Decimal | None] = mapped_column(D)
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class AuditLog(BaseUuid, Base):
    __tablename__ = 'audit_logs'
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), index=True)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), index=True)
    action: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    __table_args__ = (Index('ix_audit_actor_ts', 'actor_id', 'ts'), Index('ix_audit_action_ts', 'action', 'ts'))


class SystemFlag(Base, Timestamped):
    __tablename__ = 'system_flags'
    slug: Mapped[str] = mapped_column(String(80), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    reason: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'))


class WatcherLeaseModel(Base):
    __tablename__ = 'watcher_lease'
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    holder: Mapped[str] = mapped_column(String(80), nullable=False)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    renewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SystemIncident(BaseUuid, Base):
    __tablename__ = 'system_incidents'
    severity: Mapped[str] = mapped_column(String(16), index=True)
    code: Mapped[str] = mapped_column(String(80), index=True)
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkerHeartbeat(Base):
    __tablename__ = 'worker_heartbeats'
    worker_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    service: Mapped[str] = mapped_column(String(32), index=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    current_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
