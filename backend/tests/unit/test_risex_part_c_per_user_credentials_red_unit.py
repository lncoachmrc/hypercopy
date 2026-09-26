from __future__ import annotations

import asyncio
import inspect
import uuid
from pathlib import Path
from time import time
from types import SimpleNamespace

import pytest

from app.security.risex_order_codec import RISExPlaceOrder, build_place_order_action_hash
from app.security.risex_place_order_permit import RISExPreparedPlaceOrderPermit
from app.security.risex_place_order_request import prepare_place_order_request
from app.adapters import risex as risex_adapter_module
from app.adapters.risex_types import ProviderWriteDisabled
from app.security.risex_pre_order_gate import authorize_pre_order_probe
from app.security.risex_signed_testnet_policy import SignedTestnetPolicy
from app.services import risex_order_preparation, risex_worker_submission
from app.services.risex_execution_window import RISExOperationalWindowController
from app.models.entities import JobState
from app.workers import execution_worker
from tests.unit.risex_replay_test_support import make_test_replay_architecture_attestation


ACCOUNT = "0x" + ("11" * 20)
SIGNER = "0x" + ("22" * 20)
AUTH = "0x" + ("33" * 20)
ROUTER = "0x" + ("44" * 20)


def _source(obj: object) -> str:
    return inspect.getsource(obj)


def _request():
    now = int(time())
    order = RISExPlaceOrder(
        market_id=1,
        size_steps=100,
        price_ticks=50_000,
        side=0,
        post_only=False,
        reduce_only=False,
        stp_mode=0,
        order_type=1,
        time_in_force=3,
        client_order_id=7,
        ttl_units=0,
    )
    permit = RISExPreparedPlaceOrderPermit(
        account_address=ACCOUNT,
        signer_address=SIGNER,
        action_hash=build_place_order_action_hash(order),
        nonce_anchor=43,
        nonce_bitmap_index=0,
        deadline=now + 300,
        _signature=bytes([9]) * 65,
    )
    return prepare_place_order_request(order=order, permit=permit)


def _replay_attestation():
    return make_test_replay_architecture_attestation(
        request=_request(),
        chain_id=11155931,
        authorization_address=AUTH,
        router_address=ROUTER,
    )


def test_continuous_worker_does_not_load_global_signer_unit() -> None:
    source = _source(execution_worker.Worker._run_risex_copy_job)
    assert "load_testnet_signer_credential" not in source, (
        "RED: continuous worker still loads the global env signer"
    )
    assert "RISEX_TESTNET_ACCOUNT_ADDRESS" not in source
    assert "RISEX_TESTNET_SIGNER_PRIVATE_KEY" not in source


def test_worker_submission_does_not_read_global_account_env_unit() -> None:
    source = _source(risex_worker_submission.prepare_risex_worker_submission)
    assert "RISEX_TESTNET_ACCOUNT_ADDRESS" not in source, (
        "RED: worker submission still resolves portfolio identity from env"
    )


def test_worker_submission_does_not_hardcode_credential_active_unit() -> None:
    source = _source(risex_worker_submission.prepare_risex_worker_submission)
    compact = source.replace(" ", "").replace("\n", "")
    assert "credential_active=True" not in compact, (
        "RED: worker risk flags still hard-code credential_active=True"
    )


def test_worker_submission_uses_resolved_credential_for_portfolio_unit() -> None:
    source = _source(risex_worker_submission.prepare_risex_worker_submission)
    assert "resolved_credential.account_address" in source, (
        "RED: portfolio read is not yet bound to the per-order resolved credential"
    )


def test_request_from_plan_accepts_explicit_per_order_credential_unit() -> None:
    signature = inspect.signature(risex_order_preparation.prepare_risex_ioc_request_from_plan)
    names = set(signature.parameters)
    assert "env" not in names, (
        "RED: request-from-plan still accepts env instead of an explicit per-order credential"
    )
    assert {"credential", "resolved_credential", "signer_credential"} & names, (
        "RED: request-from-plan has no explicit per-order credential parameter"
    )


def test_pre_order_gate_accepts_public_verified_binding_without_key_unit() -> None:
    signature = inspect.signature(risex_worker_submission.build_risex_pre_order_gate_for_request)
    names = set(signature.parameters)
    assert "env" not in names, (
        "RED: continuous pre-order gate still resolves account/signer from env"
    )
    assert (
        {"credential_binding", "resolved_binding"} & names
        or {"account_address", "signer_address"} <= names
    ), "RED: continuous pre-order gate has no explicit verified public credential binding"
    assert not {"private_key", "local_account", "credential"} & names, (
        "RED: pre-order gate must receive public binding only, never secret key material"
    )


def test_continuous_callgraph_has_no_global_identity_env_reads_unit() -> None:
    sources = {
        "worker": _source(execution_worker.Worker._run_risex_copy_job),
        "submission": _source(risex_worker_submission.prepare_risex_worker_submission),
        "pre_order_gate": _source(risex_worker_submission.build_risex_pre_order_gate_for_request),
        "request_from_plan": _source(risex_order_preparation.prepare_risex_ioc_request_from_plan),
    }
    forbidden = (
        "RISEX_TESTNET_ACCOUNT_ADDRESS",
        "RISEX_TESTNET_SIGNER_PRIVATE_KEY",
        "load_testnet_signer_credential",
    )
    offenders = {
        name: [token for token in forbidden if token in source]
        for name, source in sources.items()
        if any(token in source for token in forbidden)
    }
    assert offenders == {}, f"RED: continuous callgraph still reads global RISEx identity: {offenders}"


def test_manual_prepare_path_remains_env_bound_unit() -> None:
    signature = inspect.signature(risex_order_preparation.prepare_risex_ioc_request)
    assert "env" in signature.parameters
    assert "load_testnet_signer_credential" in _source(
        risex_order_preparation.prepare_risex_ioc_request
    )


def test_manual_signed_readiness_remains_signer_bound_unit() -> None:
    from app.security import risex_signed_testnet_runner

    source = _source(risex_signed_testnet_runner.run_signed_testnet_readiness)
    assert "load_testnet_signer_credential" in source
    assert "env" in inspect.signature(
        risex_signed_testnet_runner.run_signed_testnet_readiness
    ).parameters


def test_manual_probe_scripts_remain_env_bound_unit() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    manual = (repo_root / "scripts" / "risex_manual_order_probe.py").read_text()
    replay = (repo_root / "scripts" / "risex_negative_replay_probe.py").read_text()
    readiness = (repo_root / "scripts" / "risex_signed_testnet_readiness.py").read_text()
    assert "load_testnet_signer_credential" in manual
    assert "load_testnet_signer_credential" in replay
    assert "run_signed_testnet_readiness" in readiness


def test_movefund_true_remains_authorized_under_adr0002_criterion_unit() -> None:
    now = int(time())
    policy = SignedTestnetPolicy(
        network="testnet",
        explicit_approval=True,
        deployment_verdict="PASS",
        deployment_identity_verified=True,
        disposable_account_asserted=True,
        dedicated_signer_asserted=True,
        operatorhub_bypass_disabled=True,
    )
    evidence = SimpleNamespace(
        network="testnet",
        account=ACCOUNT,
        signer=SIGNER,
        chain_id=11155931,
        auth_contract=AUTH,
        router=ROUTER,
        session_active=True,
        session_account=ACCOUNT,
        session_expiration=now + 3600,
        onchain_perps_only_scope=False,
        perps_order_succeeded=None,
        fund_movement_rejected=None,
        withdrawal_rejected=None,
        post_revoke_order_rejected=None,
        operatorhub_bypass_disabled=True,
        perps_permission=True,
        move_fund_permission=True,
        all_permission=True,
        spot_permission=True,
        fund_movement_path_absent=True,
    )
    gate = authorize_pre_order_probe(
        policy=policy,
        evidence=evidence,
        now=now,
        replay_protection_architecture_attestation=_replay_attestation(),
    )
    assert gate.authorization_criterion == "perps_permission_and_fund_movement_path_absent"
    assert gate.order_probe_allowed is True


@pytest.mark.parametrize("mismatch", ["account", "signer"])
@pytest.mark.asyncio
async def test_continuous_writer_rejects_permit_identity_mismatch_against_resolved_binding_unit(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    context_fingerprint = "part-c-c4-context"
    boot_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    window = RISExOperationalWindowController(
        worker_id="worker-a",
        boot_id=boot_id,
    )
    request_id = uuid.uuid4()
    assert window.begin_arm(
        request_id=request_id,
        control_generation=1,
        context_fingerprint=context_fingerprint,
    )
    window.open_window(
        request_id=request_id,
        control_generation=1,
        context_fingerprint=context_fingerprint,
    )

    request = _request()
    alternate_account = "0x" + ("55" * 20)
    alternate_signer = "0x" + ("66" * 20)
    resolved_account = alternate_account if mismatch == "account" else ACCOUNT
    resolved_signer = alternate_signer if mismatch == "signer" else SIGNER

    class FakeSignedTransport:
        def __init__(self) -> None:
            self.post_calls = 0

        async def prepare_place_order_post(self, prepared_request):
            return {"request": prepared_request}

        async def post_prepared_place_order(self, _payload):
            self.post_calls += 1
            return {"success": True}

        async def aclose(self) -> None:
            return None

    class FakeCloser:
        async def aclose(self) -> None:
            return None

    transport = FakeSignedTransport()
    prepared = risex_worker_submission.RISExWorkerPreparedSubmission(
        transport=transport,
        submission=SimpleNamespace(request=request),
        api=FakeCloser(),
        rpc=FakeCloser(),
        account_address=resolved_account,
        signer_address=resolved_signer,
        generation=1,
    )

    async def singleton_ok(_worker, _db) -> bool:
        return True

    async def prepare_submission(_db, _job, *, readiness_assertions):
        assert readiness_assertions == {"operatorhub_bypass_disabled": True}
        return prepared

    async def active_destination(_db, _job) -> bool:
        return True

    async def process_with_real_writer(db, adapter, job, *, submission):
        with pytest.raises(
            ProviderWriteDisabled,
            match="continuous permit (account|signer) identity mismatch",
        ):
            await adapter.place_ioc(
                db=db,
                job=job,
                request=submission.request,
            )
        return JobState.SKIPPED.value

    monkeypatch.setenv("RISEX_SIGNED_WRITES_ENABLED", "true")
    monkeypatch.setattr(
        execution_worker,
        "current_risex_context_fingerprint",
        lambda _worker, _assertions: context_fingerprint,
    )
    monkeypatch.setattr(
        execution_worker,
        "risex_singleton_matches_worker",
        singleton_ok,
    )
    monkeypatch.setattr(
        execution_worker,
        "prepare_risex_worker_submission",
        prepare_submission,
    )
    monkeypatch.setattr(
        execution_worker,
        "process_risex_job",
        process_with_real_writer,
    )
    monkeypatch.setattr(
        risex_adapter_module,
        "job_matches_active_destination",
        active_destination,
    )
    monkeypatch.setattr(
        risex_adapter_module,
        "RISExSignedTestnetHTTPTransport",
        FakeSignedTransport,
    )

    worker = SimpleNamespace(
        id="worker-a",
        boot_id=boot_id,
        risex_window=window,
        risex_submission_lock=asyncio.Lock(),
    )
    job = SimpleNamespace(
        execution_provider="risex",
        execution_network="testnet",
        execution_epoch_id=uuid.uuid4(),
    )

    result = await execution_worker.Worker._run_risex_copy_job(worker, object(), job)

    assert result == JobState.SKIPPED.value
    assert transport.post_calls == 0
