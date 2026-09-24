from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from eth_account import Account
from fastapi import HTTPException
from sqlalchemy import select, text
from starlette.requests import Request

from app.api import user as user_api
from app.db.session import SessionLocal, engine
from app.models import entities
from app.models.entities import CopyJob, JobState, User
from app.schemas import user as user_schemas
from app.services.execution_destination import job_matches_active_destination


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


MAIN_PRIVATE_KEY = "11" * 32
SIGNER_PRIVATE_KEY_1 = "22" * 32
SIGNER_PRIVATE_KEY_2 = "33" * 32
SIGNER_PRIVATE_KEY_3 = "44" * 32
SIGNER_PRIVATE_KEY_4 = "55" * 32
SIGNER_PRIVATE_KEY_5 = "66" * 32
MAIN_ACCOUNT = Account.from_key(MAIN_PRIVATE_KEY)
SIGNER_1 = Account.from_key(SIGNER_PRIVATE_KEY_1)
SIGNER_2 = Account.from_key(SIGNER_PRIVATE_KEY_2)
SIGNER_3 = Account.from_key(SIGNER_PRIVATE_KEY_3)
SIGNER_4 = Account.from_key(SIGNER_PRIVATE_KEY_4)


def _require_contract():
    schema = getattr(user_schemas, "RISExTradingAccountIn", None)
    endpoint = getattr(user_api, "link_risex_trading_account", None)
    account_type = getattr(entities, "RISExTradingAccount", None)
    credential_type = getattr(entities, "RISExSigningCredential", None)

    assert schema is not None, "RED: RISExTradingAccountIn is missing"
    assert endpoint is not None, "RED: POST /risex-trading-account endpoint is missing"
    assert account_type is not None, "RED: RISExTradingAccount model is missing"
    assert credential_type is not None, "RED: RISExSigningCredential model is missing"
    return schema, endpoint, account_type, credential_type


def _request() -> Request:
    return Request({"type": "http", "client": ("127.0.0.1", 12345), "headers": []})


def _unique_wallet() -> str:
    return "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


async def _insert_user(db, *, wallet: str | None = None) -> User:
    user_id = uuid.uuid4()
    wallet = wallet or _unique_wallet()
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


async def _cleanup_user(db, user_id: uuid.UUID) -> None:
    # Integration PostgreSQL is ephemeral. Keep committed users/audit history in
    # place instead of deleting across the append-only audit trigger boundary.
    del user_id
    await db.rollback()


def _valid_evidence(account: str, signer: str) -> SimpleNamespace:
    return SimpleNamespace(
        account=account,
        signer=signer,
        session_active=True,
        session_not_expired=True,
        perps_permission=True,
        move_fund_permission=True,
    )


def _stub_valid_verification(monkeypatch: pytest.MonkeyPatch, observed: dict) -> None:
    async def verify(*args, **kwargs):
        observed["verification_args"] = args
        observed["verification_kwargs"] = kwargs
        return _valid_evidence(
            str(kwargs["account_address"]),
            str(kwargs["signer_address"]),
        )

    monkeypatch.setattr(user_api, "_verify_risex_signer_binding", verify, raising=False)


def _stub_crypto(monkeypatch: pytest.MonkeyPatch, observed: dict, *, fail: bool = False) -> None:
    def encrypt(plaintext: str, *, user_id: str, account_id: str):
        observed.setdefault("encrypted", []).append(
            {"plaintext": plaintext, "user_id": user_id, "account_id": account_id}
        )
        if fail:
            raise RuntimeError("synthetic encryption failure")
        return SimpleNamespace(
            ciphertext_b64="ciphertext-not-plaintext",
            nonce_b64="nonce",
            wrapped_dek_b64="wrapped",
            wrap_nonce_b64="wrapnonce",
            key_provider="test",
            key_reference="test-key",
            key_version=91,
        )

    monkeypatch.setattr(user_api, "crypto", SimpleNamespace(encrypt=encrypt))


async def _risex_counts(db, user_id: uuid.UUID) -> tuple[int, int]:
    accounts = (
        await db.execute(
            text("SELECT count(*) FROM risex_trading_accounts WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
    ).scalar_one()
    credentials = (
        await db.execute(
            text(
                """
                SELECT count(*)
                FROM risex_signing_credentials c
                JOIN risex_trading_accounts a ON a.id = c.risex_trading_account_id
                WHERE a.user_id = :user_id
                """
            ),
            {"user_id": user_id},
        )
    ).scalar_one()
    return int(accounts), int(credentials)


@pytest.mark.asyncio
async def test_account_must_equal_authenticated_siwe_wallet_before_crypto_or_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema, endpoint, _account_type, _credential_type = _require_contract()
    observed: dict = {}

    async def forbidden_verify(*_args, **_kwargs):
        raise AssertionError("account mismatch must fail before provider verification")

    monkeypatch.setattr(
        user_api,
        "_verify_risex_signer_binding",
        forbidden_verify,
        raising=False,
    )
    _stub_crypto(monkeypatch, observed)

    async with SessionLocal() as db:
        user = await _insert_user(db)
        try:
            body = schema.model_validate(
                {
                    "account_address": "0x" + ("44" * 20),
                    "signer_private_key": SIGNER_PRIVATE_KEY_1,
                }
            )
            with pytest.raises(HTTPException) as exc_info:
                await endpoint(body, _request(), user, db)

            assert exc_info.value.status_code == 422
            assert observed.get("encrypted", []) == []
            assert await _risex_counts(db, user.id) == (0, 0)
            epoch_count = (
                await db.execute(
                    text("SELECT count(*) FROM execution_epochs WHERE user_id = :user_id"),
                    {"user_id": user.id},
                )
            ).scalar_one()
            assert epoch_count == 0
        finally:
            await _cleanup_user(db, user.id)
            await engine.dispose()


@pytest.mark.asyncio
async def test_main_wallet_private_key_is_rejected_before_provider_io_and_encryption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema, endpoint, _account_type, _credential_type = _require_contract()
    observed: dict = {}

    async def forbidden_verify(*_args, **_kwargs):
        raise AssertionError("main-wallet private key must fail before provider verification")

    monkeypatch.setattr(
        user_api,
        "_verify_risex_signer_binding",
        forbidden_verify,
        raising=False,
    )
    _stub_crypto(monkeypatch, observed)

    async with SessionLocal() as db:
        user = await _insert_user(db, wallet=MAIN_ACCOUNT.address.lower())
        try:
            body = schema.model_validate(
                {
                    "account_address": user.auth_wallet,
                    "signer_private_key": MAIN_PRIVATE_KEY,
                }
            )
            with pytest.raises(HTTPException) as exc_info:
                await endpoint(body, _request(), user, db)

            assert exc_info.value.status_code == 422
            assert observed.get("encrypted", []) == []
            assert await _risex_counts(db, user.id) == (0, 0)
        finally:
            await _cleanup_user(db, user.id)
            await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_active", "session_not_expired", "perps_permission"),
    [
        (False, True, True),
        (None, True, True),
        (True, False, True),
        (True, True, False),
    ],
)
async def test_onchain_binding_failures_are_fail_closed_before_crypto_or_persistence(
    monkeypatch: pytest.MonkeyPatch,
    session_active: bool | None,
    session_not_expired: bool,
    perps_permission: bool,
) -> None:
    schema, endpoint, _account_type, _credential_type = _require_contract()
    observed: dict = {}

    async def verify(*_args, **_kwargs):
        return SimpleNamespace(
            account=MAIN_ACCOUNT.address,
            signer=SIGNER_1.address,
            session_active=session_active,
            session_not_expired=session_not_expired,
            perps_permission=perps_permission,
            move_fund_permission=True,
        )

    monkeypatch.setattr(user_api, "_verify_risex_signer_binding", verify, raising=False)
    _stub_crypto(monkeypatch, observed)

    async with SessionLocal() as db:
        user = await _insert_user(db)
        try:
            body = schema.model_validate(
                {
                    "account_address": user.auth_wallet,
                    "signer_private_key": SIGNER_PRIVATE_KEY_1,
                }
            )
            with pytest.raises(HTTPException):
                await endpoint(body, _request(), user, db)

            assert observed.get("encrypted", []) == []
            assert await _risex_counts(db, user.id) == (0, 0)
        finally:
            await _cleanup_user(db, user.id)
            await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["account", "signer"])
async def test_onchain_identity_evidence_must_match_exact_requested_pair_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    schema, endpoint, _account_type, _credential_type = _require_contract()
    observed: dict = {}

    class _AsyncContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    monkeypatch.setattr(
        user_api,
        "RISExReadOnlyHTTPTransport",
        lambda **_kwargs: _AsyncContext(),
    )
    monkeypatch.setattr(
        user_api,
        "RISExReadOnlyRPCTransport",
        lambda **_kwargs: _AsyncContext(),
    )

    async def collect_runtime(*_args, **_kwargs):
        return SimpleNamespace(
            domain_verifying_contract="0x" + ("77" * 20),
            block_number=1,
        )

    def evaluate_preflight(*_args, **_kwargs):
        return SimpleNamespace(
            verdict="PASS",
            deployment_identity_verified=True,
        )

    monkeypatch.setattr(
        user_api,
        "collect_runtime_deployment_evidence",
        collect_runtime,
    )
    monkeypatch.setattr(
        user_api,
        "evaluate_pinned_deployment_preflight",
        evaluate_preflight,
    )

    async with SessionLocal() as db:
        user = await _insert_user(db)
        try:
            evidence_account = (
                SIGNER_2.address if mismatch == "account" else user.auth_wallet
            )
            evidence_signer = (
                SIGNER_2.address if mismatch == "signer" else SIGNER_1.address
            )

            async def collect_authorization(*_args, **_kwargs):
                return SimpleNamespace(
                    account=evidence_account,
                    signer=evidence_signer,
                    session_active=True,
                    session_not_expired=True,
                    perps_permission=True,
                    move_fund_permission=True,
                )

            monkeypatch.setattr(
                user_api,
                "collect_authorization_session_evidence",
                collect_authorization,
            )
            _stub_crypto(monkeypatch, observed)

            body = schema.model_validate(
                {
                    "account_address": user.auth_wallet,
                    "signer_private_key": SIGNER_PRIVATE_KEY_1,
                }
            )
            with pytest.raises(HTTPException) as exc_info:
                await endpoint(body, _request(), user, db)

            assert exc_info.value.status_code == 422
            assert observed.get("encrypted", []) == []
            assert await _risex_counts(db, user.id) == (0, 0)
            epoch_count = (
                await db.execute(
                    text("SELECT count(*) FROM execution_epochs WHERE user_id = :user_id"),
                    {"user_id": user.id},
                )
            ).scalar_one()
            assert epoch_count == 0
        finally:
            await _cleanup_user(db, user.id)
            await engine.dispose()


@pytest.mark.asyncio
async def test_success_encrypts_with_record_aad_and_binds_epoch_to_logical_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema, endpoint, account_type, credential_type = _require_contract()
    observed: dict = {}
    _stub_valid_verification(monkeypatch, observed)
    _stub_crypto(monkeypatch, observed)

    async with SessionLocal() as db:
        user = await _insert_user(db)
        try:
            body = schema.model_validate(
                {
                    "account_address": user.auth_wallet,
                    "signer_private_key": SIGNER_PRIVATE_KEY_1,
                }
            )
            await endpoint(body, _request(), user, db)

            account = (
                await db.execute(select(account_type).where(account_type.user_id == user.id))
            ).scalar_one()
            credential = (
                await db.execute(
                    select(credential_type).where(
                        credential_type.signer_address == SIGNER_1.address
                    )
                )
            ).scalar_one()
            epoch = (
                await db.execute(
                    text(
                        """
                        SELECT e.account_address, e.credential_version
                        FROM users u
                        JOIN execution_epochs e ON e.id = u.active_execution_epoch_id
                        WHERE u.id = :user_id
                        """
                    ),
                    {"user_id": user.id},
                )
            ).mappings().one()

            assert account.account_address.lower() == user.auth_wallet.lower()
            assert credential.signer_address.lower() == SIGNER_1.address.lower()
            assert credential.generation == 1
            assert credential.key_version == 91
            assert epoch["account_address"].lower() == user.auth_wallet.lower()
            assert epoch["credential_version"] == 1
            assert epoch["credential_version"] != credential.key_version

            encrypted = observed["encrypted"]
            assert len(encrypted) == 1
            assert encrypted[0]["plaintext"] == SIGNER_PRIVATE_KEY_1
            assert encrypted[0]["user_id"] == str(user.id)
            assert encrypted[0]["account_id"] == str(account.id)

            persisted_strings = [
                str(getattr(credential, column.name))
                for column in credential.__table__.columns
                if getattr(credential, column.name) is not None
            ]
            assert all(SIGNER_PRIVATE_KEY_1 not in value for value in persisted_strings)

            verification_call = repr(
                (
                    observed.get("verification_args"),
                    observed.get("verification_kwargs"),
                )
            ).lower()
            assert user.auth_wallet.lower() in verification_call
            assert SIGNER_1.address.lower() in verification_call
        finally:
            await _cleanup_user(db, user.id)
            await engine.dispose()


@pytest.mark.asyncio
async def test_encryption_failure_leaves_no_account_credential_or_epoch_partial_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema, endpoint, _account_type, _credential_type = _require_contract()
    observed: dict = {}
    _stub_valid_verification(monkeypatch, observed)
    _stub_crypto(monkeypatch, observed, fail=True)

    async with SessionLocal() as db:
        user = await _insert_user(db)
        user_id = user.id
        try:
            body = schema.model_validate(
                {
                    "account_address": user.auth_wallet,
                    "signer_private_key": SIGNER_PRIVATE_KEY_5,
                }
            )
            with pytest.raises(RuntimeError, match="synthetic encryption failure"):
                await endpoint(body, _request(), user, db)

            await db.rollback()
            assert await _risex_counts(db, user_id) == (0, 0)
            epoch_count = (
                await db.execute(
                    text("SELECT count(*) FROM execution_epochs WHERE user_id = :user_id"),
                    {"user_id": user_id},
                )
            ).scalar_one()
            assert epoch_count == 0
        finally:
            await _cleanup_user(db, user_id)
            await engine.dispose()


@pytest.mark.asyncio
async def test_rotation_increments_logical_generation_and_rejects_old_epoch_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema, endpoint, account_type, credential_type = _require_contract()
    observed: dict = {}
    _stub_valid_verification(monkeypatch, observed)
    _stub_crypto(monkeypatch, observed)

    async with SessionLocal() as db:
        user = await _insert_user(db)
        try:
            first_body = schema.model_validate(
                {
                    "account_address": user.auth_wallet,
                    "signer_private_key": SIGNER_PRIVATE_KEY_3,
                }
            )
            await endpoint(first_body, _request(), user, db)

            first_epoch = (
                await db.execute(
                    text(
                        "SELECT active_execution_epoch_id FROM users WHERE id = :user_id"
                    ),
                    {"user_id": user.id},
                )
            ).scalar_one()
            first_version = (
                await db.execute(
                    text(
                        "SELECT credential_version FROM execution_epochs WHERE id = :epoch_id"
                    ),
                    {"epoch_id": first_epoch},
                )
            ).scalar_one()

            job = CopyJob(
                user_id=user.id,
                asset="BTC",
                origin="RECONCILE",
                state=JobState.QUEUED,
                correlation_id=uuid.uuid4().hex,
                execution_epoch_id=first_epoch,
                execution_provider="risex",
                execution_network="testnet",
                context={
                    "execution_provider": "risex",
                    "follower_network": "testnet",
                },
            )
            db.add(job)
            await db.commit()

            second_body = schema.model_validate(
                {
                    "account_address": user.auth_wallet,
                    "signer_private_key": SIGNER_PRIVATE_KEY_4,
                }
            )
            await endpoint(second_body, _request(), user, db)

            second_epoch = (
                await db.execute(
                    text(
                        "SELECT active_execution_epoch_id FROM users WHERE id = :user_id"
                    ),
                    {"user_id": user.id},
                )
            ).scalar_one()
            second_version = (
                await db.execute(
                    text(
                        "SELECT credential_version FROM execution_epochs WHERE id = :epoch_id"
                    ),
                    {"epoch_id": second_epoch},
                )
            ).scalar_one()
            latest_credential = (
                await db.execute(
                    select(credential_type)
                    .where(credential_type.signer_address == SIGNER_4.address)
                    .order_by(credential_type.generation.desc())
                )
            ).scalars().first()
            account_count, credential_count = await _risex_counts(db, user.id)
            signer_addresses = (
                await db.execute(
                    select(credential_type.signer_address)
                    .join(
                        account_type,
                        credential_type.risex_trading_account_id == account_type.id,
                    )
                    .where(account_type.user_id == user.id)
                )
            ).scalars().all()

            assert account_count == 1
            assert credential_count == 1
            assert signer_addresses == [SIGNER_4.address]
            assert SIGNER_3.address not in signer_addresses
            assert latest_credential is not None
            assert first_version == 1
            assert second_version == 2
            assert latest_credential.generation == 2
            assert second_version > first_version
            assert second_version != latest_credential.key_version
            assert second_epoch != first_epoch
            assert await job_matches_active_destination(db, job) is False
        finally:
            await _cleanup_user(db, user.id)
            await engine.dispose()
