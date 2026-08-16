from pydantic import BaseModel, Field


class AdminAction(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    confirmation: str | None = Field(default=None, max_length=80)


class AdminReconcile(BaseModel):
    reason: str = Field(default='Manual reconciliation', min_length=3, max_length=500)
