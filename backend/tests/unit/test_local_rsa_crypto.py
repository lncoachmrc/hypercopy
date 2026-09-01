import base64

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.config import Settings, settings
from app.core.crypto import EnvelopeCrypto


_TEST_WRITER_ENVIRONMENT_ID = '00000000-0000-0000-0000-000000000001'


def _rsa_pair(bits: int = 3072) -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return (
        base64.b64encode(public_pem).decode('ascii'),
        base64.b64encode(private_pem).decode('ascii'),
    )


def test_local_rsa_separates_api_encrypt_from_worker_decrypt(monkeypatch):
    public_b64, private_b64 = _rsa_pair()
    monkeypatch.setattr(settings, 'APP_ENV', 'production')
    monkeypatch.setattr(settings, 'ENABLE_LIVE_TRADING', True)
    monkeypatch.setattr(settings, 'KEK_PROVIDER', 'local_rsa')
    monkeypatch.setattr(settings, 'TRAXION_KEK_PUBLIC_KEY_B64', public_b64)
    monkeypatch.setattr(settings, 'TRAXION_KEK_PRIVATE_KEY_B64', '')

    api_crypto = EnvelopeCrypto()
    blob = api_crypto.encrypt('0x' + '22' * 32, user_id='u1', account_id='a1')

    assert blob.key_provider == 'local_rsa'
    assert blob.key_reference.startswith('local_rsa:sha256:')
    assert blob.wrap_nonce_b64 is None

    with pytest.raises(RuntimeError, match='PRIVATE_KEY_B64'):
        api_crypto.decrypt(blob, user_id='u1', account_id='a1')

    monkeypatch.setattr(settings, 'TRAXION_KEK_PUBLIC_KEY_B64', '')
    monkeypatch.setattr(settings, 'TRAXION_KEK_PRIVATE_KEY_B64', private_b64)
    worker_crypto = EnvelopeCrypto()

    assert worker_crypto.decrypt(blob, user_id='u1', account_id='a1') == '0x' + '22' * 32


def test_local_rsa_fingerprint_rejects_wrong_worker_key(monkeypatch):
    public_b64, _ = _rsa_pair()
    _, wrong_private_b64 = _rsa_pair()
    monkeypatch.setattr(settings, 'APP_ENV', 'production')
    monkeypatch.setattr(settings, 'ENABLE_LIVE_TRADING', True)
    monkeypatch.setattr(settings, 'KEK_PROVIDER', 'local_rsa')
    monkeypatch.setattr(settings, 'TRAXION_KEK_PUBLIC_KEY_B64', public_b64)
    monkeypatch.setattr(settings, 'TRAXION_KEK_PRIVATE_KEY_B64', '')

    blob = EnvelopeCrypto().encrypt('secret', user_id='u1', account_id='a1')

    monkeypatch.setattr(settings, 'TRAXION_KEK_PUBLIC_KEY_B64', '')
    monkeypatch.setattr(settings, 'TRAXION_KEK_PRIVATE_KEY_B64', wrong_private_b64)
    with pytest.raises(RuntimeError, match='fingerprint'):
        EnvelopeCrypto().decrypt(blob, user_id='u1', account_id='a1')


def test_local_rsa_rejects_keys_smaller_than_3072_bits(monkeypatch):
    public_b64, _ = _rsa_pair(bits=2048)
    monkeypatch.setattr(settings, 'KEK_PROVIDER', 'local_rsa')
    monkeypatch.setattr(settings, 'TRAXION_KEK_PUBLIC_KEY_B64', public_b64)

    with pytest.raises(RuntimeError, match='at least 3072 bits'):
        EnvelopeCrypto().encrypt('secret', user_id='u1', account_id='a1')


def test_production_live_accepts_local_rsa_provider():
    cfg = Settings(
        _env_file=None,
        APP_ENV='production',
        SESSION_SECRET='s' * 32,
        ENABLE_LIVE_TRADING=True,
        KEK_PROVIDER='local_rsa',
        TRAXION_MAINNET_WRITER_ENVIRONMENT_ID=_TEST_WRITER_ENVIRONMENT_ID,
        RAILWAY_ENVIRONMENT_ID=_TEST_WRITER_ENVIRONMENT_ID,
    )
    assert cfg.KEK_PROVIDER == 'local_rsa'


def test_production_live_still_rejects_env_provider():
    with pytest.raises(ValueError, match='aws_kms or local_rsa'):
        Settings(
            _env_file=None,
            APP_ENV='production',
            SESSION_SECRET='s' * 32,
            ENABLE_LIVE_TRADING=True,
            KEK_PROVIDER='env',
        )


def test_production_live_refuses_legacy_env_wrapped_blob(monkeypatch):
    monkeypatch.setattr(settings, 'APP_ENV', 'staging')
    monkeypatch.setattr(settings, 'ENABLE_LIVE_TRADING', True)
    monkeypatch.setattr(settings, 'KEK_PROVIDER', 'env')
    monkeypatch.setattr(
        settings,
        'ENCRYPTION_KEY_B64',
        base64.b64encode(b'x' * 32).decode('ascii'),
    )
    blob = EnvelopeCrypto().encrypt('secret', user_id='u1', account_id='a1')

    monkeypatch.setattr(settings, 'APP_ENV', 'production')
    monkeypatch.setattr(settings, 'ENABLE_LIVE_TRADING', True)
    monkeypatch.setattr(settings, 'KEK_PROVIDER', 'local_rsa')
    with pytest.raises(RuntimeError, match='env-wrapped credentials are disabled'):
        EnvelopeCrypto().decrypt(blob, user_id='u1', account_id='a1')
