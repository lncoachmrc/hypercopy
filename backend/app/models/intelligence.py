from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

D = Numeric(30, 12)


class MasterStrategyProfile(Base):
    __tablename__ = 'master_strategy_profiles'

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    network: Mapped[str] = mapped_column(String(16), nullable=False)
    master_address: Mapped[str] = mapped_column(String(42), nullable=False)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    asset_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    profile: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    learned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('network', 'master_address', name='uq_master_strategy_source'),
    )


class CapitalIntelligenceDecision(Base):
    __tablename__ = 'capital_intelligence_decisions'

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(128))
    candidate_id: Mapped[str] = mapped_column(String(48), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(D)
    follower_equity: Mapped[Decimal] = mapped_column(D, nullable=False)
    eligible_equity: Mapped[Decimal] = mapped_column(D, nullable=False)
    recommended_capital: Mapped[Decimal] = mapped_column(D, nullable=False)
    coverage_pct: Mapped[Decimal] = mapped_column(D, nullable=False)
    tracking_error_pct: Mapped[Decimal] = mapped_column(D, nullable=False)
    policy: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    provider_attempts: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    __table_args__ = (
        Index('ix_capital_intelligence_user_created', 'user_id', 'created_at'),
    )
