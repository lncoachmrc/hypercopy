from datetime import UTC, datetime, timedelta

from eth_account import Account
from eth_account.messages import encode_defunct

from app.core.config import settings
from app.core.security import (
    build_signin_message,
    create_refresh_token,
    hash_refresh_token,
    normalize_address,
    verify_wallet_signature,
)


def test_wallet_signature_roundtrip():
    account=Account.create()
    now=datetime.now(UTC)
    message=build_signin_message(account.address.lower(),'abcdef1234567890',now,now+timedelta(minutes=5))
    signature=Account.sign_message(encode_defunct(text=message),account.key).signature.hex()
    assert verify_wallet_signature(account.address,message,signature)
    assert 'Sign in to TRAXION.' in message
    assert 'no permission to move funds' in message


def test_address_normalization():
    a='0x'+'AB'*20
    assert normalize_address(a)==a.lower()


def test_refresh_tokens_are_opaque_unique_and_not_stored_as_plaintext_keys():
    first=create_refresh_token()
    second=create_refresh_token()
    assert first!=second
    assert len(first)>=48
    digest=hash_refresh_token(first)
    assert digest==hash_refresh_token(first)
    assert digest!=first
    assert len(digest)==64


def test_refresh_window_is_longer_than_access_session():
    assert settings.SESSION_TTL_SECONDS==3600
    assert settings.SESSION_REFRESH_TTL_SECONDS==86400
    assert settings.SESSION_REFRESH_TTL_SECONDS>settings.SESSION_TTL_SECONDS
