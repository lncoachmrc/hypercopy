from datetime import datetime
from pydantic import BaseModel, Field, field_validator


class ChallengeIn(BaseModel):
    address: str


class ChallengeOut(BaseModel):
    message: str
    expires_at: datetime


class VerifyIn(BaseModel):
    address: str
    signature: str = Field(min_length=20, max_length=300)


class SessionUser(BaseModel):
    id: str
    auth_wallet: str
    role: str
    state: str
    copy_state: str


class SessionOut(BaseModel):
    user: SessionUser
    entitlements: dict
    csrf_token: str
