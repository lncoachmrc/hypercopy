from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings


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


class EnvelopeCrypto:
    """Envelope encryption with record-bound AAD.

    * Local/staging: KEK is a 32-byte Railway/Docker variable and wraps a random
      DEK with AES-256-GCM.
    * Production: AWS KMS `GenerateDataKey`/`Decrypt` is supported as the external
      KMS permitted by the architecture. The API role only needs GenerateDataKey;
      the execution-worker role alone needs Decrypt.
    """

    def __init__(self) -> None:
        self.provider = settings.KEK_PROVIDER

    def encrypt(self, plaintext: str, *, user_id: str, account_id: str) -> EncryptedCredential:
        aad = f'hypercopy:credential:{user_id}:{account_id}:v1'.encode()
        if self.provider == 'aws_kms':
            return self._encrypt_kms(plaintext.encode(), aad)
        return self._encrypt_env(plaintext.encode(), aad)

    def decrypt(self, blob: EncryptedCredential, *, user_id: str, account_id: str) -> str:
        aad = f'hypercopy:credential:{user_id}:{account_id}:v1'.encode()
        if blob.key_provider == 'aws_kms':
            dek = self._kms_decrypt(_unb64(blob.wrapped_dek_b64), blob.key_reference)
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
        kek = _unb64(settings.ENCRYPTION_KEY_B64)
        return AESGCM(kek).decrypt(_unb64(blob.wrap_nonce_b64 or ''), _unb64(blob.wrapped_dek_b64), b'hypercopy:dek-wrap:v1')

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
