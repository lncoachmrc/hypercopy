from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

import app.api.user as user_api
from app.db.session import SessionLocal, engine
from app.models.entities import (
    CopyState,
    CredentialStatus,
    RISExSigningCredential,
    RISExTradingAccount,
    User,
    UserState,
)
from app.schemas.user import TradingProviderIn
from app.services.execution_destination import user_destination_state

pytestmark = pytest.mark.skipif(
    os.getenv('RUN_INTEGRATION') != '1',
    reason='requires CI PostgreSQL',
)


def _wallet() -> str:
    return '0x' + uuid.uuid4().hex + uuid.uuid4().hex[:8]


async def _add_valid_risex_credential(db, user: User) -> None:
    account = RISExTradingAccount(
        user_id=user.id,
        account_address=user.auth_wallet.lower(),
    )
    db.add(account)
    await db.flush()
    db.add(
        RISExSigningCredential(
            risex_trading_account_id=account.id,
            signer_address='0x'
            + hashlib.sha256(user.id.bytes + b'never-activated-audit-risex').hexdigest()[:40],
            ciphertext_b64='risex-ciphertext',
            nonce_b64='risex-nonce',
            wrapped_dek_b64='risex-wrapped',
            wrap_nonce_b64='risex-wrapnonce',
            key_provider='test',
            key_reference='risex-test-key',
            key_version=91,
            generation=4,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            status=CredentialStatus.ACTIVE,
        )
    )
    await db.commit()


def _stub_valid_risex_onchain_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    class _AsyncContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    monkeypatch.setattr(
        user_api,
        'RISExReadOnlyHTTPTransport',
        lambda **_kwargs: _AsyncContext(),
    )
    monkeypatch.setattr(
        user_api,
        'RISExReadOnlyRPCTransport',
        lambda **_kwargs: _AsyncContext(),
    )

    async def collect_runtime(*_args, **_kwargs):
        return SimpleNamespace(
            domain_verifying_contract='0x' + ('77' * 20),
            block_number=1,
        )

    def evaluate_preflight(*_args, **_kwargs):
        return SimpleNamespace(
            verdict='PASS',
            deployment_identity_verified=True,
        )

    async def collect_authorization(*_args, **kwargs):
        return SimpleNamespace(
            account=str(kwargs['account']),
            signer=str(kwargs['signer']),
            session_active=True,
            session_not_expired=True,
            perps_permission=True,
            move_fund_permission=True,
        )

    monkeypatch.setattr(user_api, 'collect_runtime_deployment_evidence', collect_runtime)
    monkeypatch.setattr(user_api, 'evaluate_pinned_deployment_preflight', evaluate_preflight)
    monkeypatch.setattr(user_api, 'collect_authorization_session_evidence', collect_authorization)


@pytest.mark.asyncio
async def test_never_activated_provider_switch_audit_uses_null_for_missing_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('ENABLE_LIVE_TRADING', 'false')
    _stub_valid_risex_onchain_evidence(monkeypatch)
    user_id: uuid.UUID | None = None
    try:
        async with SessionLocal() as db:
            user = User(
                auth_wallet=_wallet(),
                state=UserState.ACTIVE,
                copy_state=CopyState.PAUSED,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            user_id = user.id

            before = (
                await db.execute(
                    text(
                        'SELECT execution_provider, execution_network, active_execution_epoch_id '
                        'FROM users WHERE id = :user_id'
                    ),
                    {'user_id': user.id},
                )
            ).mappings().one()
            assert before['execution_provider'] == 'hyperliquid'
            assert before['execution_network'] == 'testnet'
            assert before['active_execution_epoch_id'] is None
            await _add_valid_risex_credential(db, user)

            payload = await user_api.trading_provider(
                TradingProviderIn(provider='risex'),
                user=user,
                db=db,
            )
            current = await user_destination_state(db, user.id)
            audit_row = (
                await db.execute(
                    text(
                        "SELECT before, after FROM audit_logs "
                        "WHERE actor_id = :user_id AND subject_id = :user_id "
                        "AND action = 'TRADING_PROVIDER_CHANGED' "
                        'ORDER BY ts DESC LIMIT 1'
                    ),
                    {'user_id': user.id},
                )
            ).mappings().one()

        assert payload['execution_provider'] == 'risex'
        assert current.provider == 'risex'
        assert audit_row['before']['epoch_id'] is None
        assert audit_row['after']['epoch_id'] == str(current.epoch_id)
        assert audit_row['before']['epoch_id'] != 'None'
        assert audit_row['after']['epoch_id'] != 'None'
    finally:
        # Successful provider changes create immutable audit evidence that keeps
        # principal references. The CI database is ephemeral, so retain this
        # random fixture rather than mutating the append-only audit trail.
        await engine.dispose()
