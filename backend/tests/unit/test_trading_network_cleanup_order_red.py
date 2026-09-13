from __future__ import annotations

import inspect

from app.api import user as user_api


def test_trading_network_authorizes_transition_before_destructive_cleanup() -> None:
    source = inspect.getsource(user_api.trading_network)
    authorize_at = source.index('next_state = await set_user_network')
    delete_account_at = source.index('await db.delete(account)')
    delete_ledger_at = source.index('delete(PositionLedger)')
    delete_risk_at = source.index('await db.delete(risk_state)')

    assert authorize_at < delete_account_at
    assert authorize_at < delete_ledger_at
    assert authorize_at < delete_risk_at
