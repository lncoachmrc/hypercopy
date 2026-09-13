from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field


class AdminAction(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    confirmation: str | None = Field(default=None, max_length=80)


class AdminReconcile(BaseModel):
    reason: str = Field(default='Manual reconciliation', min_length=3, max_length=500)


class RISExExecutionControlAction(BaseModel):
    action: Literal['ARM', 'DISARM']
    target_worker_id: str = Field(min_length=1, max_length=80)
    target_boot_id: uuid.UUID
    reason: str = Field(min_length=3, max_length=500)
    confirmation: str = Field(min_length=1, max_length=80)
    disposable_account_asserted: bool = False
    dedicated_signer_asserted: bool = False
    operatorhub_bypass_disabled: bool = False
    fund_movement_path_absent: bool = False
