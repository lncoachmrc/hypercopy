from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from eth_account import Account
from eth_account.messages import encode_defunct

from app.core.config import settings

ALGORITHM = 'HS256'


def normalize_address(address: str) -> str:
    address = address.strip().lower()
    if not address.startswith('0x') or len(address) != 42:
        raise ValueError('Invalid EVM address')
    int(address[2:], 16)
    return address


def build_signin_message(address: str, nonce: str, issued_at: datetime, expires_at: datetime) -> str:
    return (
        f'{settings.SIWE_DOMAIN} wants you to sign in with your Ethereum account:\n'
        f'{address}\n\n'
        'Sign in to TRAXION. Authentication only: no blockchain transaction, no gas, and no permission to move funds.\n\n'
        f'URI: {settings.SIWE_URI}\n'
        'Version: 1\n'
        'Chain ID: 1\n'
        f'Nonce: {nonce}\n'
        f'Issued At: {issued_at.isoformat()}\n'
        f'Expiration Time: {expires_at.isoformat()}'
    )


def verify_wallet_signature(address: str, message: str, signature: str) -> bool:
    recovered = Account.recover_message(encode_defunct(text=message), signature=signature)
    return recovered.lower() == normalize_address(address)


def create_session_token(
    user_id: str,
    address: str,
    role: str,
    csrf: str | None = None,
    *,
    session_id: str | None = None,
    session_absolute_exp: int | None = None,
) -> tuple[str, str]:
    now = datetime.now(UTC)
    csrf_token = csrf or secrets.token_urlsafe(32)
    payload: dict[str, Any] = {
        'sub': user_id,
        'wallet': normalize_address(address),
        'role_hint': role,
        'csrf': csrf_token,
        'iat': int(now.timestamp()),
        'exp': int((now + timedelta(seconds=settings.SESSION_TTL_SECONDS)).timestamp()),
        'jti': secrets.token_urlsafe(18),
    }
    if session_id:
        payload['sid'] = session_id
    if session_absolute_exp is not None:
        payload['session_exp'] = int(session_absolute_exp)
    return jwt.encode(payload, settings.SESSION_SECRET, algorithm=ALGORITHM), csrf_token


def decode_session_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.SESSION_SECRET, algorithms=[ALGORITHM])


def create_refresh_token() -> str:
    """Create the first high-entropy opaque credential in a refresh family."""
    return secrets.token_urlsafe(48)


def derive_refresh_successor(token: str) -> str:
    """Derive the next opaque credential without storing its plaintext.

    A retry carrying the previous credential can reproduce the exact same
    successor during the bounded delivery-grace window. HMAC domain separation
    keeps this independent from the digest used for Redis lookup keys.
    """
    digest = hmac.new(
        settings.SESSION_SECRET.encode(),
        f'traxion-refresh-successor-v1:{token}'.encode(),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode()


def hash_refresh_token(token: str) -> str:
    return hmac.new(
        settings.SESSION_SECRET.encode(),
        token.encode(),
        hashlib.sha256,
    ).hexdigest()


def hash_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    return hashlib.sha256((settings.SESSION_SECRET[:16] + ip).encode()).hexdigest()
