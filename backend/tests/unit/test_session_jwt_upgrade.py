"""Session contracts across PyJWT upgrades; all signing keys are ephemeral."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime

import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.deps import session_claims
from app.core import security
from app.core.cookies import session_cookie_name


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    key = secrets.token_urlsafe(48)
    monkeypatch.setattr(security.settings, 'SESSION_SECRET', key)
    return key


def _b64(value: bytes) -> bytes:
    return base64.urlsafe_b64encode(value).rstrip(b'=')


def _claims() -> dict:
    now = int(datetime.now(UTC).timestamp())
    return {'sub': str(uuid.uuid4()), 'iat': now - 1, 'exp': now + 3600}


def _signed_payload(raw: bytes, key: str, *, padded: bool = False) -> str:
    # Independent standard HS256 encoder: compatibility without jwt.encode.
    header = _b64(b'{"alg":"HS256","typ":"JWT"}')
    message = header + b'.' + _b64(raw)
    signature = base64.urlsafe_b64encode(hmac.digest(key.encode(), message, hashlib.sha256))
    if not padded:
        signature = signature.rstrip(b'=')
    return (message + b'.' + signature).decode('ascii')


def test_created_session_preserves_identity_csrf_and_lifetimes():
    user_id, family = str(uuid.uuid4()), uuid.uuid4().hex
    address = '0x' + 'Ab' * 20
    absolute_expiry = int(datetime.now(UTC).timestamp()) + 86400
    token, csrf = security.create_session_token(
        user_id, address, 'USER', session_id=family,
        session_absolute_exp=absolute_expiry,
    )
    claims = security.decode_session_token(token)
    assert claims['sub'] == user_id
    assert claims['wallet'] == address.lower()
    assert claims['role_hint'] == 'USER'
    assert claims['csrf'] == csrf and csrf
    assert claims['sid'] == family
    assert claims['session_exp'] == absolute_expiry
    assert claims['exp'] - claims['iat'] == security.settings.SESSION_TTL_SECONDS
    another, _ = security.create_session_token(user_id, address, 'USER')
    assert security.decode_session_token(another)['jti'] != claims['jti']


@pytest.mark.parametrize('padded', [False, True])
def test_standard_hs256_session_format_remains_compatible(signing_key, padded):
    claims = _claims()
    token = _signed_payload(json.dumps(claims).encode(), signing_key, padded=padded)
    assert security.decode_session_token(token) == claims


@pytest.mark.parametrize('part', ['payload', 'signature'])
def test_tampered_session_is_rejected(signing_key, part):
    token = _signed_payload(json.dumps(_claims()).encode(), signing_key)
    segments = token.split('.')
    if part == 'payload':
        segments[1] = _b64(json.dumps({**_claims(), 'role_hint': 'SUPERADMIN'}).encode()).decode()
    else:
        segments[2] = ('A' if segments[2][0] != 'A' else 'B') + segments[2][1:]
    with pytest.raises(jwt.InvalidSignatureError):
        security.decode_session_token('.'.join(segments))


@pytest.mark.parametrize('algorithm', ['none', 'HS384'])
def test_session_algorithm_allowlist_remains_hs256(signing_key, algorithm):
    token = jwt.encode(_claims(), None if algorithm == 'none' else signing_key, algorithm=algorithm)
    with pytest.raises(jwt.InvalidAlgorithmError):
        security.decode_session_token(token)


@pytest.mark.parametrize('field,offset,error', [
    ('exp', -60, jwt.ExpiredSignatureError),
    ('iat', 3600, jwt.ImmatureSignatureError),
])
def test_invalid_session_times_are_rejected(signing_key, field, offset, error):
    claims = {**_claims(), field: int(datetime.now(UTC).timestamp()) + offset}
    token = _signed_payload(json.dumps(claims).encode(), signing_key)
    with pytest.raises(error):
        security.decode_session_token(token)


def test_non_alphabet_signature_junk_is_not_accepted(signing_key):
    token = _signed_payload(json.dumps(_claims()).encode(), signing_key)
    with pytest.raises(jwt.DecodeError):
        security.decode_session_token(token + '!!!!')


def test_deep_payload_raises_token_error_not_raw_recursion_error(signing_key):
    # About 20 KB, local only. Check the decoder precondition explicitly:
    # 1,500 levels did not exceed the JSON decoder limit on the CI runner.
    payload = b'{"nested":' + b'[' * 10000 + b'0' + b']' * 10000 + b'}'
    with pytest.raises(RecursionError):
        json.loads(payload)
    token = _signed_payload(payload, signing_key)
    with pytest.raises(jwt.DecodeError):
        security.decode_session_token(token)


@pytest.mark.parametrize('token', ['', 'not-a-jwt', 'a.b.c'])
@pytest.mark.asyncio
async def test_malformed_session_cookie_is_unauthorized(token):
    request = Request({
        'type': 'http', 'method': 'GET', 'path': '/api/v1/auth/session',
        'headers': [(b'cookie', f'{session_cookie_name()}={token}'.encode())],
    })
    with pytest.raises(HTTPException) as exc:
        await session_claims(request)
    assert exc.value.status_code == 401
