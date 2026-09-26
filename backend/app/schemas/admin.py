from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AdminAction(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    confirmation: str | None = Field(default=None, max_length=80)


class AdminReconcile(BaseModel):
    reason: str = Field(default='Manual reconciliation', min_length=3, max_length=500)


class RISExExecutionControlAction(BaseModel):
    model_config = ConfigDict(extra='forbid')

    action: Literal['ARM', 'DISARM']
    target_worker_id: str = Field(min_length=1, max_length=80)
    target_boot_id: uuid.UUID
    reason: str = Field(min_length=3, max_length=500)
    confirmation: str = Field(min_length=1, max_length=80)
    operatorhub_bypass_disabled: bool = False
