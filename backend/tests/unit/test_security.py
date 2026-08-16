from datetime import UTC, datetime, timedelta

from eth_account import Account
from eth_account.messages import encode_defunct

from app.core.security import build_signin_message, normalize_address, verify_wallet_signature


def test_wallet_signature_roundtrip():
    account=Account.create()
    now=datetime.now(UTC)
    message=build_signin_message(account.address.lower(),'abcdef1234567890',now,now+timedelta(minutes=5))
    signature=Account.sign_message(encode_defunct(text=message),account.key).signature.hex()
    assert verify_wallet_signature(account.address,message,signature)


def test_address_normalization():
    a='0x'+'AB'*20
    assert normalize_address(a)==a.lower()
