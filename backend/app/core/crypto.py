from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings


_LOCAL_RSA_LABEL = b'hypercopy:dek-wrap:local-rsa:v1'
_MIN_LOCAL_RSA_BITS = 3072


@dataclass(frozen=True, slots=True)
class EncryptedCredential:
    ciphertext_b64: str
    nonce_b64: str
    wrapped_dek_b64: str
    wrap_nonce_b64: str | None
    key_provider: str
    key_reference: str
    key_version: int = 1


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def _unb64(value: str) -> bytes:
    return base64.b64decode(value.encode('ascii'))


def _rsa_key_reference(public_key: rsa.RSAPublicKey) -> str:
    der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(der).hexdigest()
    return f'local_rsa:sha256:{digest}'


def _load_local_rsa_public_key() -> rsa.RSAPublicKey:
    if not settings.TRAXION_KEK_PUBLIC_KEY_B64:
        raise RuntimeError('TRAXION_KEK_PUBLIC_KEY_B64 is required for local_rsa encryption')
    try:
        key = serialization.load_pem_public_key(_unb64(settings.TRAXION_KEK_PUBLIC_KEY_B64))
    except Exception as exc:
        raise RuntimeError('TRAXION_KEK_PUBLIC_KEY_B64 must contain a base64-encoded PEM public key') from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise RuntimeError('TRAXION_KEK_PUBLIC_KEY_B64 must contain an RSA public key')
    if key.key_size < _MIN_LOCAL_RSA_BITS:
        raise RuntimeError(f'local_rsa requires an RSA key of at least {_MIN_LOCAL_RSA_BITS} bits')
    return key


def _load_local_rsa_private_key() -> rsa.RSAPrivateKey:
    if not settings.TRAXION_KEK_PRIVATE_KEY_B64:
        raise RuntimeError('TRAXION_KEK_PRIVATE_KEY_B64 is required for local_rsa decryption')
    try:
        key = serialization.load_pem_private_key(
            _unb64(settings.TRAXION_KEK_PRIVATE_KEY_B64),
            password=None,
        )
    except Exception as exc:
        raise RuntimeError('TRAXION_KEK_PRIVATE_KEY_B64 must contain a base64-encoded unencrypted PEM private key') from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise RuntimeError('TRAXION_KEK_PRIVATE_KEY_B64 must contain an RSA private key')
    if key.key_size < _MIN_LOCAL_RSA_BITS:
        raise RuntimeError(f'local_rsa requires an RSA key of at least {_MIN_LOCAL_RSA_BITS} bits')
    return key


class EnvelopeCrypto:
    """Envelope encryption with record-bound AAD.

    * Local/staging: KEK is a 32-byte Railway/Docker variable and wraps a random
      DEK with AES-256-GCM.
    * Production option 1: AWS KMS `GenerateDataKey`/`Decrypt`. The API role only
      needs GenerateDataKey; the execution-worker role alone needs Decrypt.
    * Production option 2: `local_rsa`. The API receives only an RSA public key
      and wraps the DEK with RSA-OAEP/SHA-256; only the execution-worker receives
      the matching private key and can unwrap it.
    """

    def __init__(self) -> None:
        self.provider = settings.KEK_PROVIDER

    def encrypt(self, plaintext: str, *, user_id: str, account_id: str) -> EncryptedCredential:
        aad = f'hypercopy:credential:{user_id}:{account_id}:v1'.encode()
        if self.provider == 'aws_kms':
            return self._encrypt_kms(plaintext.encode(), aad)
        if self.provider == 'local_rsa':
            return self._encrypt_local_rsa(plaintext.encode(), aad)
        if self.provider == 'env':
            return self._encrypt_env(plaintext.encode(), aad)
        raise ValueError('Unsupported key provider')

    def decrypt(self, blob: EncryptedCredential, *, user_id: str, account_id: str) -> str:
        aad = f'hypercopy:credential:{user_id}:{account_id}:v1'.encode()
        if (
            settings.APP_ENV == 'production'
            and settings.ENABLE_LIVE_TRADING
            and blob.key_provider == 'env'
        ):
            raise RuntimeError('env-wrapped credentials are disabled for production live execution; re-link the API Wallet')
        if blob.key_provider == 'aws_kms':
            dek = self._kms_decrypt(_unb64(blob.wrapped_dek_b64), blob.key_reference)
        elif blob.key_provider == 'local_rsa':
            dek = self._local_rsa_decrypt(_unb64(blob.wrapped_dek_b64), blob.key_reference)
        elif blob.key_provider == 'env':
            dek = self._unwrap_env(blob, user_id=user_id, account_id=account_id)
        else:
            raise ValueError('Unsupported key provider')
        plain = AESGCM(dek).decrypt(_unb64(blob.nonce_b64), _unb64(blob.ciphertext_b64), aad)
        return plain.decode()

    def _encrypt_env(self, plaintext: bytes, aad: bytes) -> EncryptedCredential:
        if not settings.ENCRYPTION_KEY_B64:
            raise RuntimeError('ENCRYPTION_KEY_B64 is required for env KEK provider')
        kek = _unb64(settings.ENCRYPTION_KEY_B64)
        if len(kek) != 32:
            raise RuntimeError('ENCRYPTION_KEY_B64 must decode to exactly 32 bytes')
        dek = os.urandom(32)
        nonce = os.urandom(12)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
        wrap_nonce = os.urandom(12)
        wrapped = AESGCM(kek).encrypt(wrap_nonce, dek, b'hypercopy:dek-wrap:v1')
        return EncryptedCredential(_b64(ciphertext), _b64(nonce), _b64(wrapped), _b64(wrap_nonce), 'env', 'railway:ENCRYPTION_KEY_B64')

    def _unwrap_env(self, blob: EncryptedCredential, *, user_id: str, account_id: str) -> bytes:
        del user_id, account_id
        if not settings.ENCRYPTION_KEY_B64:
            raise RuntimeError('ENCRYPTION_KEY_B64 is required for env KEK provider')
        kek = _unb64(settings.ENCRYPTION_KEY_B64)
        if len(kek) != 32:
            raise RuntimeError('ENCRYPTION_KEY_B64 must decode to exactly 32 bytes')
        return AESGCM(kek).decrypt(_unb64(blob.wrap_nonce_b64 or ''), _unb64(blob.wrapped_dek_b64), b'hypercopy:dek-wrap:v1')

    def _encrypt_local_rsa(self, plaintext: bytes, aad: bytes) -> EncryptedCredential:
        public_key = _load_local_rsa_public_key()
        dek = os.urandom(32)
        nonce = os.urandom(12)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
        wrapped = public_key.encrypt(
            dek,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=_LOCAL_RSA_LABEL,
            ),
        )
        return EncryptedCredential(
            _b64(ciphertext),
            _b64(nonce),
            _b64(wrapped),
            None,
            'local_rsa',
            _rsa_key_reference(public_key),
        )

    def _local_rsa_decrypt(self, wrapped: bytes, key_reference: str) -> bytes:
        rsa_secret = _load_local_rsa_private_key()
        actual_reference = _rsa_key_reference(rsa_secret.public_key())
        if key_reference != actual_reference:
            raise RuntimeError('local_rsa key fingerprint does not match the credential key reference')
        return rsa_secret.decrypt(
            wrapped,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=_LOCAL_RSA_LABEL,
            ),
        )

    def _kms_client(self):
        import boto3  # optional runtime dependency used only by aws_kms provider
        kwargs = {'region_name': settings.AWS_REGION} if settings.AWS_REGION else {}
        return boto3.client('kms', **kwargs)

    def _encrypt_kms(self, plaintext: bytes, aad: bytes) -> EncryptedCredential:
        if not settings.ENCRYPTION_KEY_REFERENCE:
            raise RuntimeError('ENCRYPTION_KEY_REFERENCE must contain the KMS key id/arn')
        response = self._kms_client().generate_data_key(KeyId=settings.ENCRYPTION_KEY_REFERENCE, KeySpec='AES_256')
        dek = bytes(response['Plaintext'])
        wrapped = bytes(response['CiphertextBlob'])
        nonce = os.urandom(12)
        ciphertext = AESGCM(dek).encrypt(nonce, plaintext, aad)
        # bytearray best-effort wiping is not reliable in CPython; keep lifetime short.
        return EncryptedCredential(_b64(ciphertext), _b64(nonce), _b64(wrapped), None, 'aws_kms', settings.ENCRYPTION_KEY_REFERENCE)

    def _kms_decrypt(self, wrapped: bytes, key_reference: str) -> bytes:
        response = self._kms_client().decrypt(CiphertextBlob=wrapped, KeyId=key_reference)
        return bytes(response['Plaintext'])


crypto = EnvelopeCrypto()
