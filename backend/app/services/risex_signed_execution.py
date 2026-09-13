from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from app.adapters.risex import RISExAdapter
from app.adapters.risex_signed_testnet_http import (
    RISExPreOrderFreshnessProbe,
    RISExSignedTestnetHTTPTransport,
)
from app.security.risex_deployment_runtime import PublicAPITransport, PublicRPCTransport
from app.security.risex_pre_order_gate import (
    RISExPreOrderProbeGate,
    assert_pre_order_probe_gate_attested,
)
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
from app.security.risex_signed_testnet_runner import (
    RuntimeClock,
    _system_clock,
    assert_runtime_readiness_attested,
    run_signed_testnet_readiness,
)


@dataclass(slots=True)
class RISExSignedExecutionSession:
    """Short-lived process-local owner of one attested RISEx signed transport."""

    adapter: RISExAdapter
    _transport: RISExSignedTestnetHTTPTransport

    async def __aenter__(self) -> 'RISExSignedExecutionSession':
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        await self._transport.aclose()


async def arm_risex_signed_testnet_execution(
    *,
    env: Mapping[str, str],
    api: PublicAPITransport,
    rpc: PublicRPCTransport,
    pre_order_gate: RISExPreOrderProbeGate,
    freshness_probe: RISExPreOrderFreshnessProbe,
    explicit_approval: bool,
    disposable_account_asserted: bool,
    dedicated_signer_asserted: bool,
    operatorhub_bypass_disabled: bool,
    fund_movement_path_absent: bool = False,
    readiness_clock: RuntimeClock = _system_clock,
    transport_client: httpx.AsyncClient | None = None,
) -> RISExSignedExecutionSession:
    """Arm one explicit RISEx testnet signed-execution session.

    The caller must supply a genuine pre-order gate. Readiness is evaluated exactly
    once for each arm attempt; expiry never triggers an automatic refresh. The
    existing adapter and signed transport remain the write and live-freshness
    enforcement boundaries.
    """

    assert_pre_order_probe_gate_attested(pre_order_gate)

    result = await run_signed_testnet_readiness(
        env=env,
        api=api,
        rpc=rpc,
        network='testnet',
        explicit_approval=explicit_approval,
        disposable_account_asserted=disposable_account_asserted,
        dedicated_signer_asserted=dedicated_signer_asserted,
        operatorhub_bypass_disabled=operatorhub_bypass_disabled,
        fund_movement_path_absent=fund_movement_path_absent,
        clock=readiness_clock,
    )
    if result.report.verdict != 'PASS' or result.attestation is None:
        raise SignedTestnetBlocked('RISEx signed execution requires readiness PASS attestation')

    assert_runtime_readiness_attested(
        result.attestation,
        account_address=pre_order_gate.account_address,
        signer_address=pre_order_gate.signer_address,
        clock=readiness_clock,
    )
    if result.report.authorization_address.lower() != pre_order_gate.deployment_auth_contract.lower():
        raise SignedTestnetBlocked(
            'RISEx readiness authorization address does not match the pre-order gate'
        )
    if result.report.router_address.lower() != pre_order_gate.deployment_router.lower():
        raise SignedTestnetBlocked('RISEx readiness router address does not match the pre-order gate')

    transport = RISExSignedTestnetHTTPTransport(
        gate=pre_order_gate,
        client=transport_client,
        freshness_probe=freshness_probe,
    )
    try:
        adapter = RISExAdapter(
            network='testnet',
            transport=transport,
            readiness_attestation=result.attestation,
            readiness_clock=readiness_clock,
        )
        return RISExSignedExecutionSession(adapter=adapter, _transport=transport)
    except Exception:
        await transport.aclose()
        raise
