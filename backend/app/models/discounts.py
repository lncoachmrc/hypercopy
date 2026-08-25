from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.entities import BaseUuid, Timestamped


class UserPlanDiscount(BaseUuid, Timestamped, Base):
    __tablename__ = 'user_plan_discounts'

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True, nullable=False)
    plan_slug: Mapped[str] = mapped_column(String(32), nullable=False)
    percent_off: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey('users.id', ondelete='SET NULL'))

    __table_args__ = (
        UniqueConstraint('user_id', 'plan_slug', name='uq_user_plan_discount'),
        CheckConstraint('percent_off >= 1 AND percent_off <= 100', name='ck_user_plan_discount_percent'),
    )
