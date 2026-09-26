from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.admin import RISExExecutionControlAction
from app.services import risex_admin_extension, risex_execution_worker_extension
from app.services.risex_execution_window import RISExOperationalWindowController
from app.workers import execution_worker


def _fixed_credential(account_byte: str, signer_byte: str) -> SimpleNamespace:
    return SimpleNamespace(
        account_address="0x" + (account_byte * 40),
        signer_address="0x" + (signer_byte * 40),
    )


def _worker() -> SimpleNamespace:
    return SimpleNamespace(id="worker-a", boot_id=uuid.UUID("11111111-1111-1111-1111-111111111111"))


def _fingerprint(monkeypatch: pytest.MonkeyPatch, *, account_byte: str = "1", signer_byte: str = "2", assertions=None) -> str:
    monkeypatch.setenv("RISEX_TESTNET_ACCOUNT_ADDRESS", "0x" + (account_byte * 40))
    monkeypatch.setenv("RISEX_TESTNET_SIGNER_PRIVATE_KEY", signer_byte * 64)
    return risex_execution_worker_extension._current_context_fingerprint(
        _worker(),
        assertions or {"operatorhub_bypass_disabled": True},
    )


def test_fingerprint_excludes_per_user_identity_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _fingerprint(monkeypatch, account_byte="1", signer_byte="2")
    second = _fingerprint(monkeypatch, account_byte="3", signer_byte="4")
    assert first == second, "RED: worker-global fingerprint still includes per-user identity"


def test_fingerprint_ignores_legacy_arm_assertions_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = _fingerprint(
        monkeypatch,
        assertions={
            "operatorhub_bypass_disabled": True,
            "disposable_account_asserted": False,
            "dedicated_signer_asserted": False,
            "fund_movement_path_absent": False,
        },
    )
    changed = _fingerprint(
        monkeypatch,
        assertions={
            "operatorhub_bypass_disabled": True,
            "disposable_account_asserted": True,
            "dedicated_signer_asserted": True,
            "fund_movement_path_absent": True,
        },
    )
    assert baseline == changed, "RED: legacy ARM assertions still affect the fingerprint"


@pytest.mark.parametrize(
    ("env_name", "first", "second"),
    [
        ("RAILWAY_GIT_COMMIT_SHA", "build-a", "build-b"),
        ("RAILWAY_ENVIRONMENT_NAME", "env-a", "env-b"),
        ("RISEX_SIGNED_WRITES_ENABLED", "false", "true"),
    ],
)
def test_fingerprint_changes_with_static_global_material_unit(
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    first: str,
    second: str,
) -> None:
    monkeypatch.setenv(env_name, first)
    one = risex_execution_worker_extension._current_context_fingerprint(
        _worker(), {"operatorhub_bypass_disabled": True}
    )
    monkeypatch.setenv(env_name, second)
    two = risex_execution_worker_extension._current_context_fingerprint(
        _worker(), {"operatorhub_bypass_disabled": True}
    )
    assert one != two


def test_fingerprint_does_not_include_runtime_pause_flags_unit() -> None:
    source = inspect.getsource(risex_execution_worker_extension._current_context_fingerprint)
    assert "global_pause" not in source
    assert "emergency_stop" not in source
    assert "authorization_invalidation_epoch" not in source
    assert "last_control_generation" not in source


def test_arm_finalization_survives_begin_arm_control_generation_advance_unit() -> None:
    window = RISExOperationalWindowController(
        worker_id="worker-a",
        boot_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
    )
    request_id = uuid.uuid4()
    fingerprint = "static-context"
    assert window.begin_arm(
        request_id=request_id,
        control_generation=41,
        context_fingerprint=fingerprint,
    )
    assert window.last_control_generation == 41
    assert window.can_finalize_arm(
        request_id=request_id,
        control_generation=41,
        context_fingerprint=fingerprint,
    )


def test_arm_required_assertions_reduce_to_operatorhub_only_unit() -> None:
    assert risex_execution_worker_extension._REQUIRED_ARM_ASSERTIONS == (
        "operatorhub_bypass_disabled",
    ), "RED: continuous ARM still requires legacy single-account assertions"


def test_arm_schema_rejects_legacy_assertion_fields_unit() -> None:
    payload = {
        "action": "ARM",
        "target_worker_id": "worker-a",
        "target_boot_id": str(uuid.uuid4()),
        "reason": "part c RED",
        "confirmation": "ARM RISEX",
        "operatorhub_bypass_disabled": True,
        "disposable_account_asserted": True,
    }
    with pytest.raises(ValidationError):
        RISExExecutionControlAction.model_validate(payload)


def test_arm_schema_exposes_only_operatorhub_assertion_unit() -> None:
    fields = set(RISExExecutionControlAction.model_fields)
    assertion_fields = {
        name
        for name in fields
        if name.endswith("_asserted")
        or name == "operatorhub_bypass_disabled"
        or name == "fund_movement_path_absent"
    }
    assert assertion_fields == {"operatorhub_bypass_disabled"}, (
        f"RED: ARM schema still exposes legacy assertions: {sorted(assertion_fields)}"
    )


def test_admin_arm_persists_only_operatorhub_assertion_unit() -> None:
    source = inspect.getsource(risex_admin_extension.install_risex_admin)
    for legacy in (
        "disposable_account_asserted",
        "dedicated_signer_asserted",
        "fund_movement_path_absent",
    ):
        assert legacy not in source, f"RED: admin ARM still persists legacy assertion {legacy}"


def test_worker_does_not_fabricate_legacy_arm_assertions_unit() -> None:
    source = inspect.getsource(execution_worker.Worker._run_risex_copy_job)
    for legacy in (
        "disposable_account_asserted",
        "dedicated_signer_asserted",
        "fund_movement_path_absent",
    ):
        assert legacy not in source, f"RED: worker still fabricates {legacy}=True"


def _require_global_readiness_contract():
    runner = getattr(
        risex_execution_worker_extension,
        "run_global_risex_continuous_readiness",
        None,
    )
    attestation = getattr(
        risex_execution_worker_extension,
        "RISExGlobalContinuousReadinessAttestation",
        None,
    )
    assert runner is not None, "RED: signerless global continuous readiness runner is missing"
    assert attestation is not None, "RED: signerless global continuous readiness attestation is missing"
    return runner, attestation


def test_global_readiness_never_loads_user_identity_unit() -> None:
    runner, _attestation = _require_global_readiness_contract()
    signature = inspect.signature(runner)
    names = set(signature.parameters)
    assert not {"account", "account_address", "signer", "signer_address", "private_key"} & names
    source = inspect.getsource(runner)
    assert "load_testnet_signer_credential" not in source
    assert "RISEX_TESTNET_ACCOUNT_ADDRESS" not in source
    assert "RISEX_TESTNET_SIGNER_PRIVATE_KEY" not in source


@pytest.mark.parametrize(
    "required_material",
    [
        "PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT",
        "RISEX_SIGNED_WRITES_ENABLED",
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        "operatorhub_bypass_disabled",
    ],
)
def test_global_readiness_contains_required_fail_closed_gate_unit(required_material: str) -> None:
    runner, _attestation = _require_global_readiness_contract()
    source = inspect.getsource(runner)
    assert required_material in source, (
        f"RED: global continuous readiness does not enforce {required_material}"
    )


def test_continuous_readiness_no_longer_calls_manual_signer_bound_runner_unit() -> None:
    source = inspect.getsource(risex_execution_worker_extension._run_readiness)
    assert "run_signed_testnet_readiness" not in source, (
        "RED: continuous ARM readiness still delegates to signer-bound manual readiness"
    )


def test_manual_signer_bound_readiness_remains_unchanged_unit() -> None:
    from app.security import risex_signed_testnet_runner

    source = inspect.getsource(risex_signed_testnet_runner.run_signed_testnet_readiness)
    assert "load_testnet_signer_credential" in source
    assert "env" in inspect.signature(
        risex_signed_testnet_runner.run_signed_testnet_readiness
    ).parameters
