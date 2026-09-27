"""RED contracts for STOP2-A RISEx activation alignment.

Production code is intentionally absent in this PR.  These tests fix the
provider-neutral reconciliation boundary that the GREEN implementation must
satisfy without duplicating Hyperliquid planning logic.
"""

from __future__ import annotations

import inspect
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.api import activation
from app.services import reconcile


def _require_symbol(module, name: str):
    value = getattr(module, name, None)
    assert callable(value), f"RED: missing symbol {module.__name__}.{name}"
    return value


@asynccontextmanager
async def _no_op_lock(_user_id):
    yield


@pytest.mark.asyncio
async def test_risex_activation_uses_shared_reconciliation_planner_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    user = SimpleNamespace(id=uuid.uuid4())
    observation = SimpleNamespace(provider="normalized", network="testnet")
    master_positions = {"BTC": Decimal("0.25")}
    shared = _require_symbol(reconcile, "reconcile_observed_follower")
    hl_reader = _require_symbol(reconcile, "read_hyperliquid_reconcile_observation")
    risex_alignment = _require_symbol(activation, "_resume_risex_alignment")
    assert shared is not None and hl_reader is not None

    calls: list[str] = []

    async def shared_spy(*_args, **kwargs):
        obs = kwargs.get("observation")
        calls.append(str(getattr(obs, "provider", "unknown")))
        return {"status": "OK", "jobs_created": 0}

    async def hyperliquid_reader(*_args, **_kwargs):
        return SimpleNamespace(provider="hyperliquid", network="testnet")

    monkeypatch.setattr(reconcile, "is_master_source_user", lambda _user: False)
    monkeypatch.setattr(reconcile, "position_ledger_lock", _no_op_lock)
    monkeypatch.setattr(reconcile, "read_hyperliquid_reconcile_observation", hyperliquid_reader)
    monkeypatch.setattr(reconcile, "reconcile_observed_follower", shared_spy)
    monkeypatch.setattr(activation, "reconcile_observed_follower", shared_spy, raising=False)

    await reconcile.reconcile_user(
        object(),
        SimpleNamespace(network="testnet"),
        user,
        master_positions=master_positions,
        master_equity=Decimal("1000"),
        mids={"BTC": "100"},
    )
    await risex_alignment(
        object(),
        user,
        observation=SimpleNamespace(provider="risex", network="testnet"),
        master_positions=master_positions,
        master_equity=Decimal("1000"),
        master_mids={"BTC": "100"},
        master_configs=None,
        create_jobs=True,
    )

    assert calls == ["hyperliquid", "risex"], (
        "RED: Hyperliquid reconcile_user() and the RISEx activation branch must "
        "delegate behaviorally to the same reconcile_observed_follower callable"
    )


def test_risex_activation_alignment_only_creates_copyjobs_before_worker_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    forbidden = {
        "place_ioc",
        "claim_risex_first_post",
        "crypto.decrypt",
        "resolve_risex_worker_credential",
        "RISExSignedTestnetHTTPTransport",
    }
    risex_alignment = _require_symbol(activation, "_resume_risex_alignment")
    source = inspect.getsource(risex_alignment)
    endpoint_source = inspect.getsource(activation.resume_copy_immediate)
    combined = source + "\n" + endpoint_source
    assert not [name for name in forbidden if name in combined], (
        "RED: RISEx activation may read provider state and create/publish CopyJobs, "
        "but signed transport, first-POST claim and credential decryption belong to the worker"
    )


@pytest.mark.asyncio
async def test_risex_activation_contract_does_not_require_flat_master_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    user = SimpleNamespace(id=uuid.uuid4())
    observation = SimpleNamespace(provider="risex", network="testnet")
    risex_alignment = _require_symbol(activation, "_resume_risex_alignment")
    _require_symbol(reconcile, "reconcile_observed_follower")

    observed_master_positions: list[dict[str, Decimal]] = []

    async def shared_spy(*_args, **kwargs):
        observed_master_positions.append(dict(kwargs["master_positions"]))
        return {"status": "OK", "jobs_created": 1}

    monkeypatch.setattr(activation, "reconcile_observed_follower", shared_spy, raising=False)
    result = await risex_alignment(
        object(),
        user,
        observation=observation,
        master_positions={"BTC": Decimal("0.25")},
        master_equity=Decimal("1000"),
        master_mids={"BTC": "100"},
        master_configs=None,
        create_jobs=True,
    )

    assert observed_master_positions == [{"BTC": Decimal("0.25")}]
    assert result["status"] == "OK"


def test_risex_activation_environment_gate_precedes_provider_reads_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    _require_symbol(activation, "assert_risex_environment_allowed")
    _require_symbol(activation, "read_risex_reconcile_observation")
    source = inspect.getsource(activation.resume_copy_immediate)
    gate_index = source.index("assert_risex_environment_allowed(")
    read_index = source.index("read_risex_reconcile_observation(")
    assert gate_index < read_index, (
        "RED: assert_risex_environment_allowed() must run before the first RISEx provider read"
    )


def test_hyperliquid_reconcile_public_contract_remains_unchanged_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    signature = inspect.signature(reconcile.reconcile_user)
    assert list(signature.parameters) == [
        "db",
        "hl",
        "user",
        "master_positions",
        "master_equity",
        "mids",
        "master_mids",
        "master_configs",
        "create_jobs",
    ]
    assert signature.parameters["master_positions"].kind is inspect.Parameter.KEYWORD_ONLY
    source = inspect.getsource(reconcile.reconcile_user)
    assert "is_master_source_user(user)" in source
    assert "position_ledger_lock(user.id)" in source