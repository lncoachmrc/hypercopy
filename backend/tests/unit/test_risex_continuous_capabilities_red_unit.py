from __future__ import annotations

import importlib
import inspect

import pytest

from app.adapters import risex_signed_testnet_http


def _require_capability_module():
    try:
        return importlib.import_module("app.security.risex_continuous_capabilities")
    except ModuleNotFoundError:
        pytest.fail(
            "RED: app.security.risex_continuous_capabilities must be the single verified source "
            "for continuous fund_movement_path_absent",
            pytrace=False,
        )


def test_continuous_fund_movement_path_absent_has_one_structurally_verified_source_unit() -> None:
    module = _require_capability_module()
    capability = getattr(module, "FUND_MOVEMENT_PATH_ABSENT", None)
    assert capability is True, (
        "RED: continuous fund_movement_path_absent must come from the verified capability module"
    )

    assert risex_signed_testnet_http._ALLOWED_POST_PATHS == frozenset(
        {"/v1/orders/place"}
    )

    transport = risex_signed_testnet_http.RISExSignedTestnetHTTPTransport
    public_write_methods = {
        name
        for name, value in transport.__dict__.items()
        if callable(value)
        and not name.startswith("_")
        and name not in {"aclose"}
    }
    assert public_write_methods == {
        "prepare_place_order_post",
        "post_prepared_place_order",
        "post_place_order",
    }

    source = inspect.getsource(risex_signed_testnet_http)
    for forbidden in (
        "/withdraw",
        "/transfer",
        "/move",
        "move_fund",
        "moveFund",
        "withdraw_funds",
        "transfer_funds",
    ):
        assert forbidden not in source, (
            f"continuous signed transport unexpectedly exposes fund movement token: {forbidden}"
        )
