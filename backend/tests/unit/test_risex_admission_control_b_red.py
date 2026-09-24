from __future__ import annotations

import inspect

import pytest

from app.api import user as user_api
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
from app.services import risex_order_preparation


def _shared_environment_gate():
    gate = getattr(risex_order_preparation, "assert_risex_environment_allowed", None)
    assert gate is not None, (
        "RED: RISEx API and worker must share one raw-environment admission gate"
    )
    return gate


def _env(live: str | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if live is not None:
        values["ENABLE_LIVE_TRADING"] = live
    return values


def test_shared_risex_environment_gate_matches_worker_fail_closed_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _shared_environment_gate()
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        False,
    )

    # The isolated test stack is the only pre-acceptance path that may admit RISEx.
    gate(network="testnet", env=_env("false"))

    # Any real-capital/live interpretation is blocked on every network until ADR-0006.
    for network in ("testnet", "mainnet"):
        with pytest.raises(SignedTestnetBlocked):
            gate(network=network, env=_env("true"))

    # Mainnet stays blocked even when ENABLE_LIVE_TRADING is explicitly false.
    with pytest.raises(SignedTestnetBlocked):
        gate(network="mainnet", env=_env("false"))

    # Missing or malformed values are intentionally interpreted as live/real-capital.
    for malformed in (None, "", "0", "1", "TRUE", "False", "garbage"):
        with pytest.raises(SignedTestnetBlocked):
            gate(network="testnet", env=_env(malformed))


def test_shared_risex_environment_gate_allows_live_only_after_adr_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _shared_environment_gate()
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        True,
    )

    # This test covers only the environment/ADR admission matrix. Mainnet still
    # requires the separate transport/runtime redesign guarded elsewhere.
    gate(network="testnet", env=_env("true"))
    gate(network="mainnet", env=_env("true"))


def test_worker_write_gate_reuses_shared_environment_gate() -> None:
    source = inspect.getsource(risex_order_preparation.assert_risex_worker_write_allowed)
    assert "assert_risex_environment_allowed" in source, (
        "RED: worker and API must not maintain divergent ENABLE_LIVE_TRADING parsers"
    )


def test_post_risex_account_applies_environment_gate_before_provider_io_or_crypto() -> None:
    source = inspect.getsource(user_api.link_risex_trading_account)
    gate_index = source.index("assert_risex_environment_allowed")
    verify_index = source.index("_verify_risex_signer_binding")
    crypto_index = source.index("crypto.encrypt")
    destination_index = source.index("set_user_destination")

    assert gate_index < verify_index < crypto_index < destination_index


def test_put_risex_provider_applies_environment_gate_before_verification_and_destination() -> None:
    source = inspect.getsource(user_api.trading_provider)
    gate_index = source.index("assert_risex_environment_allowed")
    verify_index = source.index("_verify_risex_signer_binding")
    destination_index = source.index("set_user_destination")

    assert gate_index < verify_index < destination_index
