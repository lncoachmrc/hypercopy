from __future__ import annotations

import base64
import importlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from eth_account import Account
from sqlalchemy import or_, select, text
from starlette.requests import Request

import app.api.user as user_api
from app.core.config import settings
from app.core.crypto import EnvelopeCrypto
from app.db.session import SessionLocal, engine
from app.models.entities import (
    AuditLog,
    CopyJob,
    CredentialStatus,
    ExecutionEpoch,
    JobState,
    RISExSigningCredential,
    RISExTradingAccount,
    User,
)
from app.schemas.user import RISExTradingAccountIn
from app.security.risex_deployment_preflight import canonical_deployment_fingerprint
from app.security.risex_deployment_runtime import collect_runtime_deployment_evidence
from tests.unit.test_risex_signed_testnet_runner import FakeAPI, FakeRPC


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


def _wallet() -> str:
    return "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


def _private_key() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _request() -> Request:
    return Request({"type": "http", "client": ("127.0.0.1", 12345), "headers": []})


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


def _require_credentials_contract():
    try:
        module = importlib.import_module("app.services.risex_worker_credentials")
    except ModuleNotFoundError:
        pytest.fail(
            "RED: app.services.risex_worker_credentials is required for per-order DB credential resolution",
            pytrace=False,
        )
    resolver = getattr(module, "resolve_risex_worker_credential", None)
    error_type = getattr(module, "RISExWorkerCredentialResolutionError", None)
    assert resolver is not None, "RED: resolve_risex_worker_credential is missing"
    assert error_type is not None, "RED: RISExWorkerCredentialResolutionError is missing"
    return module, resolver, error_type


async def _seed_binding(
    *,
    generation: int = 1,
    epoch_version: int | None = None,
    status: CredentialStatus = CredentialStatus.ACTIVE,
    expires_at: datetime | None = None,
    account_address: str | None = None,
    epoch_account_address: str | None = None,
    private_key: str | None = None,
) -> dict[str, object]:
    user_id = uuid.uuid4()
    wallet = account_address or _wallet()
    key = private_key or _private_key()
    signer = Account.from_key(key)
    account_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    job_id = uuid.uuid4()
    expiry = expires_at if expires_at is not None else datetime.now(UTC) + timedelta(hours=1)

    async with SessionLocal() as db:
        await db.execute(
            text(
                """
                INSERT INTO users (
                    id, auth_wallet, role, state, copy_state, manual_trade_policy,
                    execution_network, network_started_at, execution_provider,
                    created_at, updated_at
                ) VALUES (
                    :user_id, :wallet, 'USER', 'ACTIVE', 'PAUSED', 'COEXIST',
                    'testnet', now(), 'risex', now(), now()
                )
                """
            ),
            {"user_id": user_id, "wallet": wallet},
        )
        db.add(
            RISExTradingAccount(
                id=account_id,
                user_id=user_id,
                account_address=wallet,
            )
        )
        db.add(
            RISExSigningCredential(
                risex_trading_account_id=account_id,
                signer_address=signer.address,
                ciphertext_b64="ciphertext",
                nonce_b64="nonce",
                wrapped_dek_b64="wrapped",
                wrap_nonce_b64="wrapnonce",
                key_provider="test",
                key_reference="test-key",
                key_version=7,
                generation=generation,
                expires_at=expiry,
                status=status,
            )
        )
        db.add(
            ExecutionEpoch(
                id=epoch_id,
                user_id=user_id,
                provider="risex",
                network="testnet",
                account_address=epoch_account_address or wallet,
                credential_version=epoch_version if epoch_version is not None else generation,
                started_at=datetime.now(UTC),
            )
        )
        await db.flush()
        await db.execute(
            text(
                "UPDATE users SET active_execution_epoch_id = :epoch_id WHERE id = :user_id"
            ),
            {"epoch_id": epoch_id, "user_id": user_id},
        )
        db.add(
            CopyJob(
                id=job_id,
                user_id=user_id,
                execution_epoch_id=epoch_id,
                execution_provider="risex",
                execution_network="testnet",
                asset="BTC",
                origin="EVENT",
                state=JobState.PROCESSING,
                owner="part-c-red",
                attempt_count=1,
                correlation_id=uuid.uuid4().hex,
                context={"execution_provider": "risex", "follower_network": "testnet"},
            )
        )
        await db.commit()

    return {
        "user_id": user_id,
        "wallet": wallet,
        "private_key": key,
        "signer": signer.address,
        "account_id": account_id,
        "epoch_id": epoch_id,
        "job_id": job_id,
        "generation": generation,
    }


def _patch_decrypt(
    monkeypatch: pytest.MonkeyPatch,
    module,
    *,
    private_key: str,
    observed: dict,
) -> None:
    def decrypt(_blob, *, user_id: str, account_id: str) -> str:
        observed["user_id"] = user_id
        observed["account_id"] = account_id
        return private_key

    crypto_obj = getattr(module, "crypto", None)
    assert crypto_obj is not None, (
        "RED: resolver must use app.core.crypto as a patchable verifier dependency"
    )
    monkeypatch.setattr(crypto_obj, "decrypt", decrypt)


async def _patch_real_verifiers_with_fake_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = await collect_runtime_deployment_evidence(
        FakeAPI(),
        FakeRPC(permissions={1: True, 2: True, 3: True, 4: True}),
        network="testnet",
    )
    fingerprint = canonical_deployment_fingerprint(expected)
    assert fingerprint is not None
    monkeypatch.setattr(
        user_api,
        "PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT",
        fingerprint,
    )
    monkeypatch.setattr(
        user_api,
        "RISExReadOnlyHTTPTransport",
        lambda **_kwargs: _AsyncContext(FakeAPI()),
    )
    monkeypatch.setattr(
        user_api,
        "RISExReadOnlyRPCTransport",
        lambda **_kwargs: _AsyncContext(
            FakeRPC(permissions={1: True, 2: True, 3: True, 4: True})
        ),
    )


def _configure_real_envelope(monkeypatch: pytest.MonkeyPatch) -> EnvelopeCrypto:
    # The admission gate intentionally reads the raw process environment rather
    # than the parsed settings object. Make the test an explicit isolated-testnet
    # context before exercising the real POST /risex-trading-account path.
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    monkeypatch.setattr(settings, "APP_ENV", "development")
    monkeypatch.setattr(settings, "ENABLE_LIVE_TRADING", False)
    monkeypatch.setattr(settings, "KEK_PROVIDER", "env")
    monkeypatch.setattr(
        settings,
        "ENCRYPTION_KEY_B64",
        base64.b64encode(b"part-c-red-envelope-key-material!"[:32]).decode("ascii"),
    )
    crypto = EnvelopeCrypto()
    crypto_module = importlib.import_module("app.core.crypto")
    monkeypatch.setattr(crypto_module, "crypto", crypto)
    monkeypatch.setattr(user_api, "crypto", crypto)
    return crypto


async def _insert_unlinked_user() -> User:
    user_id = uuid.uuid4()
    wallet = _wallet()
    async with SessionLocal() as db:
        await db.execute(
            text(
                """
                INSERT INTO users (
                    id, auth_wallet, role, state, copy_state, manual_trade_policy,
                    execution_network, network_started_at, execution_provider,
                    created_at, updated_at
                ) VALUES (
                    :user_id, :wallet, 'USER', 'ACTIVE', 'PAUSED', 'COEXIST',
                    'testnet', now(), 'hyperliquid', now(), now()
                )
                """
            ),
            {"user_id": user_id, "wallet": wallet},
        )
        await db.commit()
        return (await db.execute(select(User).where(User.id == user_id))).scalar_one()


async def _link_real_credential(
    *,
    user_id: uuid.UUID,
    private_key: str,
) -> dict[str, object]:
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        body = RISExTradingAccountIn.model_validate(
            {
                "account_address": user.auth_wallet,
                "signer_private_key": private_key,
            }
        )
        await user_api.link_risex_trading_account(body, _request(), user, db)
        account = (
            await db.execute(
                select(RISExTradingAccount).where(
                    RISExTradingAccount.user_id == user_id
                )
            )
        ).scalar_one()
        credential = (
            await db.execute(
                select(RISExSigningCredential).where(
                    RISExSigningCredential.risex_trading_account_id == account.id
                )
            )
        ).scalar_one()
        epoch_id = (
            await db.execute(
                text(
                    "SELECT active_execution_epoch_id FROM users WHERE id = :user_id"
                ),
                {"user_id": user_id},
            )
        ).scalar_one()
        job = CopyJob(
            id=uuid.uuid4(),
            user_id=user_id,
            execution_epoch_id=epoch_id,
            execution_provider="risex",
            execution_network="testnet",
            asset="BTC",
            origin="EVENT",
            state=JobState.PROCESSING,
            owner="part-c-real-envelope",
            attempt_count=1,
            correlation_id=uuid.uuid4().hex,
            context={"execution_provider": "risex", "follower_network": "testnet"},
        )
        db.add(job)
        await db.commit()
        return {
            "user_id": user_id,
            "wallet": user.auth_wallet,
            "account_id": account.id,
            "credential_id": credential.id,
            "job_id": job.id,
            "signer": credential.signer_address,
        }


@pytest.mark.asyncio
async def test_per_order_resolver_uses_active_epoch_account_generation_and_aad_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        seeded = await _seed_binding()
        module, resolver, _error_type = _require_credentials_contract()
        observed: dict = {}
        _patch_decrypt(
            monkeypatch,
            module,
            private_key=str(seeded["private_key"]),
            observed=observed,
        )
        poison_key = _private_key()
        monkeypatch.setenv("RISEX_TESTNET_ACCOUNT_ADDRESS", _wallet())
        monkeypatch.setenv("RISEX_TESTNET_SIGNER_PRIVATE_KEY", poison_key)

        async with SessionLocal() as db:
            job = await db.get(CopyJob, seeded["job_id"])
            resolved = await resolver(db, job)

        assert resolved.account_address.lower() == str(seeded["wallet"]).lower()
        assert resolved.signer_address.lower() == str(seeded["signer"]).lower()
        assert resolved.generation == seeded["generation"]
        assert resolved.local_account.address.lower() == str(seeded["signer"]).lower()
        assert observed == {
            "user_id": str(seeded["user_id"]),
            "account_id": str(seeded["account_id"]),
        }
        assert Account.from_key(poison_key).address.lower() != resolved.signer_address.lower()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_per_order_resolver_rejects_epoch_account_mismatch_dead_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        seeded = await _seed_binding(epoch_account_address=_wallet())
        module, resolver, error_type = _require_credentials_contract()
        observed: dict = {}
        _patch_decrypt(
            monkeypatch,
            module,
            private_key=str(seeded["private_key"]),
            observed=observed,
        )
        async with SessionLocal() as db:
            job = await db.get(CopyJob, seeded["job_id"])
            with pytest.raises(error_type) as exc:
                await resolver(db, job)
        assert exc.value.job_state == JobState.DEAD
        assert observed == {}, "RED: mismatch must fail before credential decryption"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_generation_mismatch_on_still_active_epoch_is_dead_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        seeded = await _seed_binding(generation=2, epoch_version=1)
        module, resolver, error_type = _require_credentials_contract()
        observed: dict = {}
        _patch_decrypt(
            monkeypatch,
            module,
            private_key=str(seeded["private_key"]),
            observed=observed,
        )
        async with SessionLocal() as db:
            job = await db.get(CopyJob, seeded["job_id"])
            with pytest.raises(error_type) as exc:
                await resolver(db, job)
        assert exc.value.job_state == JobState.DEAD
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_generation_mismatch_after_epoch_superseded_is_skipped_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        seeded = await _seed_binding(generation=2, epoch_version=1)
        new_epoch_id = uuid.uuid4()
        async with SessionLocal() as db:
            await db.execute(
                text(
                    "UPDATE execution_epochs SET ended_at = now() WHERE id = :old_epoch"
                ),
                {"old_epoch": seeded["epoch_id"]},
            )
            db.add(
                ExecutionEpoch(
                    id=new_epoch_id,
                    user_id=seeded["user_id"],
                    provider="risex",
                    network="testnet",
                    account_address=str(seeded["wallet"]),
                    credential_version=2,
                    started_at=datetime.now(UTC),
                )
            )
            await db.flush()
            await db.execute(
                text(
                    "UPDATE users SET active_execution_epoch_id = :new_epoch WHERE id = :user_id"
                ),
                {"new_epoch": new_epoch_id, "user_id": seeded["user_id"]},
            )
            await db.commit()

        module, resolver, error_type = _require_credentials_contract()
        observed: dict = {}
        _patch_decrypt(
            monkeypatch,
            module,
            private_key=str(seeded["private_key"]),
            observed=observed,
        )
        async with SessionLocal() as db:
            job = await db.get(CopyJob, seeded["job_id"])
            with pytest.raises(error_type) as exc:
                await resolver(db, job)
        assert exc.value.job_state == JobState.SKIPPED
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("status", "expiry"),
    [
        (CredentialStatus.REVOKED, timedelta(hours=1)),
        (CredentialStatus.EXPIRED, timedelta(hours=1)),
        (CredentialStatus.ACTIVE, timedelta(seconds=-1)),
    ],
)
@pytest.mark.asyncio
async def test_nonusable_or_expired_credential_is_skipped_integration(
    monkeypatch: pytest.MonkeyPatch,
    status: CredentialStatus,
    expiry: timedelta,
) -> None:
    try:
        seeded = await _seed_binding(
            status=status,
            expires_at=datetime.now(UTC) + expiry,
        )
        module, resolver, error_type = _require_credentials_contract()
        observed: dict = {}
        _patch_decrypt(
            monkeypatch,
            module,
            private_key=str(seeded["private_key"]),
            observed=observed,
        )
        async with SessionLocal() as db:
            job = await db.get(CopyJob, seeded["job_id"])
            with pytest.raises(error_type) as exc:
                await resolver(db, job)
        assert exc.value.job_state == JobState.SKIPPED
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_derived_signer_mismatch_is_dead_and_secret_safe_integration(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    try:
        seeded = await _seed_binding()
        module, resolver, error_type = _require_credentials_contract()
        secret = _private_key()
        observed: dict = {}
        _patch_decrypt(monkeypatch, module, private_key=secret, observed=observed)

        async with SessionLocal() as db:
            job = await db.get(CopyJob, seeded["job_id"])
            with pytest.raises(error_type) as exc:
                await resolver(db, job)

        assert exc.value.job_state == JobState.DEAD
        assert secret not in str(exc.value)
        assert secret not in caplog.text
        assert not hasattr(resolver, "_cached_local_account")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_per_order_resolver_real_envelope_roundtrip_and_cross_user_aad_integration(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    try:
        _configure_real_envelope(monkeypatch)
        await _patch_real_verifiers_with_fake_transports(monkeypatch)

        user_a = await _insert_unlinked_user()
        user_b = await _insert_unlinked_user()
        secret_a = _private_key()
        secret_b = _private_key()

        linked_a = await _link_real_credential(
            user_id=user_a.id,
            private_key=secret_a,
        )
        linked_b = await _link_real_credential(
            user_id=user_b.id,
            private_key=secret_b,
        )

        async with SessionLocal() as db:
            credential_a = await db.get(
                RISExSigningCredential,
                linked_a["credential_id"],
            )
            credential_b = await db.get(
                RISExSigningCredential,
                linked_b["credential_id"],
            )
            assert credential_a is not None
            assert credential_b is not None
            credential_b.ciphertext_b64 = credential_a.ciphertext_b64
            credential_b.nonce_b64 = credential_a.nonce_b64
            credential_b.wrapped_dek_b64 = credential_a.wrapped_dek_b64
            credential_b.wrap_nonce_b64 = credential_a.wrap_nonce_b64
            credential_b.key_provider = credential_a.key_provider
            credential_b.key_reference = credential_a.key_reference
            credential_b.key_version = credential_a.key_version
            await db.commit()

        module, resolver, error_type = _require_credentials_contract()
        crypto_module = importlib.import_module("app.core.crypto")
        real_crypto = user_api.crypto
        monkeypatch.setattr(module, "crypto", real_crypto, raising=False)
        monkeypatch.setattr(crypto_module, "crypto", real_crypto)

        async with SessionLocal() as db:
            job_a = await db.get(CopyJob, linked_a["job_id"])
            resolved_a = await resolver(db, job_a)
        assert resolved_a.signer_address.lower() == str(linked_a["signer"]).lower()
        assert resolved_a.local_account.address.lower() == str(linked_a["signer"]).lower()

        async with SessionLocal() as db:
            job_b = await db.get(CopyJob, linked_b["job_id"])
            with pytest.raises(error_type) as exc:
                await resolver(db, job_b)
            persisted_job = await db.get(CopyJob, linked_b["job_id"])
            audit_rows = (
                await db.execute(
                    select(AuditLog).where(
                        or_(
                            AuditLog.actor_id == linked_b["user_id"],
                            AuditLog.subject_id == linked_b["user_id"],
                        )
                    )
                )
            ).scalars().all()

        assert exc.value.job_state == JobState.DEAD
        assert str(exc.value) == "RISEx credential could not be decrypted"
        exposed = "\n".join(
            [
                str(exc.value),
                caplog.text,
                str(getattr(persisted_job, "last_error", None)),
                repr(
                    [
                        {
                            "action": row.action,
                            "reason": row.reason,
                            "before": row.before,
                            "after": row.after,
                        }
                        for row in audit_rows
                    ]
                ),
            ]
        )
        for secret in (secret_a, secret_b):
            assert secret not in exposed
    finally:
        await engine.dispose()
