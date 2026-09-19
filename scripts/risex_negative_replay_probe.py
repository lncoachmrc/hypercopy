#!/usr/bin/env python3
"""Deliberately submit the same signed RISEx testnet order payload at most twice.

SECURITY EXCEPTION: this tool exists only to test provider replay rejection on
RISEx testnet. It deliberately does not re-run the ordinary replay-protection
architecture collector after the first accepted POST, because that collector
would correctly block the now-consumed nonce before the provider can be tested.

This exception is local to this disposable-account, minimum-size negative probe.
It is not a precedent for bypassing any gate in the manual probe, CopyJob path,
continuous execution, Hyperliquid, RISEx mainnet, or any other runtime path.
The existing manual probe remains one-shot and is not imported or modified here.

Live execution requires the dedicated
--approve-two-identical-signed-order-submissions flag and separate operator
authorization. Merely implementing this file does not authorize execution.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Mapping, Sequence

import httpx

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.adapters.risex_http import RISExReadOnlyHTTPTransport  # noqa: E402
from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport  # noqa: E402
from app.adapters.risex_types import ProviderDataMalformed, ProviderReadUnavailable, ProviderWriteDisabled  # noqa: E402
from app.security.risex_authorization_session import collect_authorization_session_evidence  # noqa: E402
from app.security.risex_consumed_nonce_evidence import collect_consumed_nonce_evidence  # noqa: E402
from app.security.risex_deployment_preflight import evaluate_pinned_deployment_preflight  # noqa: E402
from app.security.risex_deployment_runtime import (  # noqa: E402
    PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    RISExReadOnlyRPCTransport,
    collect_runtime_deployment_evidence,
)
from app.security.risex_pre_order_gate import authorize_pre_order_probe  # noqa: E402
from app.security.risex_replay_protection_architecture import (  # noqa: E402
    RISExReplayProtectionArchitectureAttestation,
    collect_replay_protection_architecture_attestation,
)
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked, SignedTestnetPolicy  # noqa: E402
from app.security.risex_signer_probe import RISExSignerCapabilityEvidence  # noqa: E402
from app.security.risex_testnet_signer import load_testnet_signer_credential  # noqa: E402
from app.services.risex_order_preparation import make_freshness_probe, prepare_risex_ioc_request  # noqa: E402
from app.services.risex_signed_execution import arm_risex_signed_testnet_execution  # noqa: E402


_TESTNET_API_URL = 'https://api.testnet.rise.trade'
_TESTNET_RPC_URL = 'https://testnet.riselabs.xyz'
_RISEX_TESTNET_CHAIN_ID = 11155931
_DEFAULT_SLIPPAGE_BPS = 25

MAX_SUBMISSIONS = 2
REPLAY_DEADLINE_SECONDS = 60
MIN_REPLAY_DEADLINE_MARGIN_SECONDS = 15

_SENSITIVE_KEY_FRAGMENTS = (
    'private_key',
    'signature',
    'seed',
    'secret',
    'cookie',
    'authorization',
    'auth_header',
    'token',
)
_NONCE_REPLAY_CODES = frozenset({
    'NONCE_ALREADY_USED',
    'NONCE_USED',
    'PERMIT_NONCE_ALREADY_USED',
    'PERMIT_NONCE_USED',
    'REPLAY',
    'REPLAY_REJECTED',
})


@dataclass(slots=True)
class _SubmissionBudget:
    limit: int = MAX_SUBMISSIONS
    consumed: int = 0

    def consume_before_post(self) -> int:
        if self.limit != MAX_SUBMISSIONS:
            raise SignedTestnetBlocked('RISEx replay probe submission budget is invalid')
        if self.consumed >= self.limit:
            raise SignedTestnetBlocked('RISEx replay probe hard submission budget exhausted')
        self.consumed += 1
        return self.consumed


def negative_replay_probe_intent() -> SimpleNamespace:
    return SimpleNamespace(symbol='BTC', side='BUY', use_min_order_size=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'RISEx testnet negative replay probe: deliberately submit the same '
            'signed minimum-size order payload at most twice. No retries.'
        )
    )
    parser.add_argument(
        '--approve-two-identical-signed-order-submissions',
        action='store_true',
        default=False,
        help=(
            'Explicitly authorize up to two POST attempts containing the same '
            'signed RISEx testnet order payload.'
        ),
    )
    parser.add_argument('--disposable-account-asserted', action='store_true')
    parser.add_argument('--dedicated-signer-asserted', action='store_true')
    parser.add_argument('--operatorhub-bypass-disabled', action='store_true')
    parser.add_argument('--fund-movement-path-absent', action='store_true')
    return parser


def _sensitive_key(name: object) -> bool:
    if not isinstance(name, str):
        return False
    lowered = name.lower()
    if lowered == 'permit':
        return True
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def sanitize_diagnostic_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: sanitize_diagnostic_payload(item)
            for key, item in value.items()
            if not _sensitive_key(key)
        }
    if isinstance(value, list):
        return [sanitize_diagnostic_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_diagnostic_payload(item) for item in value]
    return value


def _assert_testnet_runtime(*, network: str, chain_id: int, api_base_url: str) -> None:
    if (
        network != 'testnet'
        or chain_id != _RISEX_TESTNET_CHAIN_ID
        or api_base_url.rstrip('/') != _TESTNET_API_URL
    ):
        raise SignedTestnetBlocked(
            'RISEx negative replay probe is testnet-only and requires the pinned testnet chain'
        )


def _canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    try:
        rendered = json.dumps(
            payload,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SignedTestnetBlocked(
            'RISEx replay probe wire payload is not canonically serializable'
        ) from exc
    return rendered.encode('utf-8')


def _payload_fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_payload_bytes(payload)).hexdigest()


def behavioral_replay_rejection_proven_for(result: str) -> bool:
    return result == 'REPLAY_REJECTED_NONCE'


def _cause_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        next_exc = current.__cause__
        if next_exc is None and not current.__suppress_context__:
            next_exc = current.__context__
        current = next_exc
    return chain


def _http_status_error(exc: BaseException) -> httpx.HTTPStatusError | None:
    for item in _cause_chain(exc):
        if isinstance(item, httpx.HTTPStatusError):
            return item
    return None


def _ambiguous_transport_error(exc: BaseException) -> bool:
    for item in _cause_chain(exc):
        if isinstance(item, (httpx.TimeoutException, httpx.TransportError)):
            return True
        if isinstance(item, httpx.HTTPStatusError):
            return item.response.status_code >= 500
    return False


def _provider_error_body(exc: BaseException) -> dict[str, Any] | None:
    status_error = _http_status_error(exc)
    if status_error is None:
        return None
    try:
        body = status_error.response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _provider_rejection_is_nonce_attributable(exc: BaseException) -> bool:
    status_error = _http_status_error(exc)
    if status_error is None or not 400 <= status_error.response.status_code < 500:
        return False

    body = _provider_error_body(exc) or {}
    code = body.get('code')
    if isinstance(code, str) and code.strip().upper() in _NONCE_REPLAY_CODES:
        return True

    message = body.get('message')
    if not isinstance(message, str):
        return False
    normalized = ' '.join(message.lower().split())
    return (
        'replay' in normalized
        or (
            'nonce' in normalized
            and ('already used' in normalized or 'consumed' in normalized)
            and ('permit' in normalized or 'witness' in normalized)
        )
    )


def _provider_failure_details(exc: BaseException) -> dict[str, Any]:
    status_error = _http_status_error(exc)
    result: dict[str, Any] = {'error_type': type(exc).__name__}
    if status_error is not None:
        result['http_status'] = status_error.response.status_code
        body = _provider_error_body(exc)
        if body is not None:
            code = body.get('code')
            message = body.get('message')
            if isinstance(code, (str, int)):
                result['provider_error_code'] = code
            if isinstance(message, str):
                result['provider_error_message'] = message[:512]
    elif _ambiguous_transport_error(exc):
        result['transport_outcome'] = 'ambiguous'
    return sanitize_diagnostic_payload(result)


def _response_identifiers(response: dict[str, Any]) -> dict[str, Any]:
    allowed = ('success', 'order_id', 'sc_order_id', 'tx_hash', 'transaction_hash')
    return {
        key: response[key]
        for key in allowed
        if key in response and isinstance(response[key], (str, int, bool, type(None)))
    }


def _provider_response_accepted(response: object) -> bool:
    return isinstance(response, dict) and response.get('success') is True


def _result(
    *,
    result: str,
    budget: _SubmissionBudget,
    first_payload_fingerprint: str | None = None,
    second_payload_fingerprint: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'result': result,
        'submission_count': budget.consumed,
        'behavioral_replay_rejection_proven': behavioral_replay_rejection_proven_for(result),
    }
    if first_payload_fingerprint is not None:
        payload['first_payload_fingerprint'] = first_payload_fingerprint
    if second_payload_fingerprint is not None:
        payload['second_payload_fingerprint'] = second_payload_fingerprint
    if extra:
        payload.update(extra)
    return sanitize_diagnostic_payload(payload)


async def _execute_replay_sequence(
    *,
    request: Any,
    transport: Any,
    collect_consumed_nonce: Callable[..., Awaitable[Any]],
    validate_second_preconditions: Callable[..., Awaitable[Any]],
) -> dict[str, Any]:
    budget = _SubmissionBudget()

    first_payload = await transport.prepare_place_order_post(request)
    if not isinstance(first_payload, dict):
        raise SignedTestnetBlocked('RISEx replay probe first wire payload is not an object')
    first_fingerprint = _payload_fingerprint(first_payload)

    budget.consume_before_post()
    try:
        first_response = await transport.post_prepared_place_order(first_payload)
    except SignedTestnetBlocked as exc:
        outcome = (
            'FIRST_SUBMISSION_AMBIGUOUS'
            if _ambiguous_transport_error(exc)
            else 'FIRST_SUBMISSION_REJECTED'
        )
        return _result(
            result=outcome,
            budget=budget,
            first_payload_fingerprint=first_fingerprint,
            extra=_provider_failure_details(exc),
        )

    if not _provider_response_accepted(first_response):
        return _result(
            result='FIRST_SUBMISSION_REJECTED',
            budget=budget,
            first_payload_fingerprint=first_fingerprint,
            extra={'first_response': _response_identifiers(first_response)},
        )

    evidence = await collect_consumed_nonce(
        account=request.permit.account_address,
        nonce_anchor=request.permit.nonce_anchor,
        nonce_bitmap_index=request.permit.nonce_bitmap_index,
    )
    if (
        getattr(evidence, 'is_nonce_used', None) is not True
        or getattr(evidence, 'bitmap_consistent', None) is not True
    ):
        return _result(
            result='NONCE_CONSUMPTION_INCONSISTENT',
            budget=budget,
            first_payload_fingerprint=first_fingerprint,
        )

    try:
        await validate_second_preconditions(request=request, evidence=evidence)
    except SignedTestnetBlocked as exc:
        outcome = (
            'DEADLINE_WINDOW_LOST'
            if 'DEADLINE_WINDOW_LOST' in str(exc)
            else 'SECOND_PRECONDITION_FAILED'
        )
        return _result(
            result=outcome,
            budget=budget,
            first_payload_fingerprint=first_fingerprint,
        )

    second_payload = await transport.prepare_place_order_post(request)
    if not isinstance(second_payload, dict):
        raise SignedTestnetBlocked('RISEx replay probe second wire payload is not an object')
    second_fingerprint = _payload_fingerprint(second_payload)
    if second_fingerprint != first_fingerprint:
        return _result(
            result='PAYLOAD_IDENTITY_MISMATCH',
            budget=budget,
            first_payload_fingerprint=first_fingerprint,
            second_payload_fingerprint=second_fingerprint,
        )

    budget.consume_before_post()
    try:
        second_response = await transport.post_prepared_place_order(second_payload)
    except SignedTestnetBlocked as exc:
        status_error = _http_status_error(exc)
        if _ambiguous_transport_error(exc):
            outcome = 'SECOND_SUBMISSION_AMBIGUOUS'
        elif status_error is not None and 400 <= status_error.response.status_code < 500:
            outcome = (
                'REPLAY_REJECTED_NONCE'
                if _provider_rejection_is_nonce_attributable(exc)
                else 'REPLAY_REJECTED_UNSPECIFIED'
            )
        else:
            outcome = 'SECOND_SUBMISSION_AMBIGUOUS'
        return _result(
            result=outcome,
            budget=budget,
            first_payload_fingerprint=first_fingerprint,
            second_payload_fingerprint=second_fingerprint,
            extra=_provider_failure_details(exc),
        )

    if _provider_response_accepted(second_response):
        return _result(
            result='REPLAY_ACCEPTED_SECURITY_FAILURE',
            budget=budget,
            first_payload_fingerprint=first_fingerprint,
            second_payload_fingerprint=second_fingerprint,
            extra={
                'first_response': _response_identifiers(first_response),
                'second_response': _response_identifiers(second_response),
            },
        )

    return _result(
        result='REPLAY_REJECTED_UNSPECIFIED',
        budget=budget,
        first_payload_fingerprint=first_fingerprint,
        second_payload_fingerprint=second_fingerprint,
        extra={'second_response': _response_identifiers(second_response)},
    )


async def _build_pre_order_gate(
    *,
    env: Mapping[str, str],
    api: RISExReadOnlyHTTPTransport,
    rpc: RISExReadOnlyRPCTransport,
    disposable_account_asserted: bool,
    dedicated_signer_asserted: bool,
    replay_protection_architecture_attestation: RISExReplayProtectionArchitectureAttestation,
    operatorhub_bypass_disabled: bool,
    fund_movement_path_absent: bool,
) -> object:
    credential = load_testnet_signer_credential(env)
    deployment = await collect_runtime_deployment_evidence(api, rpc, network='testnet')
    deployment_report = evaluate_pinned_deployment_preflight(
        deployment,
        expected_fingerprint=PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    )
    if (
        deployment.block_number is None
        or deployment.api_chain_id is None
        or deployment.domain_verifying_contract is None
        or deployment.system_router is None
    ):
        raise SignedTestnetBlocked('RISEx negative replay deployment evidence is incomplete')

    authorization = await collect_authorization_session_evidence(
        rpc,
        authorization_address=deployment.domain_verifying_contract,
        account=credential.account_address,
        signer=credential.signer_address,
        block_tag=hex(deployment.block_number),
    )
    policy = SignedTestnetPolicy(
        network='testnet',
        explicit_approval=True,
        deployment_verdict=deployment_report.verdict,
        deployment_identity_verified=deployment_report.deployment_identity_verified,
        disposable_account_asserted=disposable_account_asserted,
        dedicated_signer_asserted=dedicated_signer_asserted,
        operatorhub_bypass_disabled=operatorhub_bypass_disabled,
    )
    evidence = RISExSignerCapabilityEvidence(
        network='testnet',
        account=credential.account_address,
        signer=credential.signer_address,
        chain_id=deployment.api_chain_id,
        auth_contract=deployment.domain_verifying_contract,
        router=deployment.system_router,
        session_active=authorization.session_active,
        session_account=authorization.account,
        session_expiration=authorization.session_expiration,
        onchain_perps_only_scope=authorization.perps_only_scope,
        perps_order_succeeded=None,
        fund_movement_rejected=None,
        withdrawal_rejected=None,
        post_revoke_order_rejected=None,
        operatorhub_bypass_disabled=operatorhub_bypass_disabled,
        perps_permission=authorization.perps_permission,
        fund_movement_path_absent=fund_movement_path_absent,
    )
    return authorize_pre_order_probe(
        policy=policy,
        evidence=evidence,
        now=authorization.block_timestamp,
        replay_protection_architecture_attestation=replay_protection_architecture_attestation,
    )


async def _validate_second_submission_preconditions(
    *,
    api: RISExReadOnlyHTTPTransport,
    rpc: RISExReadOnlyRPCTransport,
    request: Any,
    replay_architecture: RISExReplayProtectionArchitectureAttestation,
) -> SimpleNamespace:
    deployment = await collect_runtime_deployment_evidence(api, rpc, network='testnet')
    deployment_report = evaluate_pinned_deployment_preflight(
        deployment,
        expected_fingerprint=PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT,
    )
    if (
        deployment_report.verdict != 'PASS'
        or deployment_report.deployment_identity_verified is not True
        or deployment_report.observed_fingerprint is None
        or deployment_report.observed_fingerprint.lower()
        != replay_architecture.deployment_fingerprint.lower()
    ):
        raise SignedTestnetBlocked(
            'RISEx negative replay deployment changed before second submission'
        )
    if (
        deployment.block_number is None
        or deployment.api_chain_id != replay_architecture.chain_id
        or deployment.domain_verifying_contract is None
        or deployment.system_router is None
        or deployment.domain_verifying_contract.lower()
        != replay_architecture.authorization_address.lower()
        or deployment.system_router.lower() != replay_architecture.router_address.lower()
    ):
        raise SignedTestnetBlocked(
            'RISEx negative replay deployment identity changed before second submission'
        )

    authorization = await collect_authorization_session_evidence(
        rpc,
        authorization_address=deployment.domain_verifying_contract,
        account=request.permit.account_address,
        signer=request.permit.signer_address,
        block_tag=hex(deployment.block_number),
    )
    if authorization.session_active is not True:
        raise SignedTestnetBlocked(
            'RISEx negative replay signer session is not active before replay'
        )
    if (
        authorization.account.lower() != request.permit.account_address.lower()
        or authorization.session_expiration < request.permit.deadline
    ):
        raise SignedTestnetBlocked(
            'RISEx negative replay signer identity/lifecycle changed before replay'
        )

    deadline_margin_seconds = request.permit.deadline - authorization.block_timestamp
    if deadline_margin_seconds < MIN_REPLAY_DEADLINE_MARGIN_SECONDS:
        raise SignedTestnetBlocked('DEADLINE_WINDOW_LOST')

    return SimpleNamespace(
        deadline_margin_seconds=deadline_margin_seconds,
        deployment_unchanged=True,
        session_active=True,
        identity_unchanged=True,
        block_number=deployment.block_number,
    )


async def _execute_once(args: argparse.Namespace) -> dict[str, Any]:
    if os.environ.get('RISEX_SIGNED_WRITES_ENABLED') != 'true':
        raise SignedTestnetBlocked(
            'RISEX_SIGNED_WRITES_ENABLED must be explicitly true for the replay probe'
        )

    required_assertions = (
        args.disposable_account_asserted,
        args.dedicated_signer_asserted,
        args.operatorhub_bypass_disabled,
        args.fund_movement_path_absent,
    )
    if not all(required_assertions):
        raise SignedTestnetBlocked(
            'RISEx replay probe requires all explicit testnet safety assertions'
        )

    intent = negative_replay_probe_intent()
    async with RISExReadOnlyHTTPTransport(base_url=_TESTNET_API_URL) as api:
        async with RISExReadOnlyRPCTransport(rpc_url=_TESTNET_RPC_URL) as rpc:
            request = await prepare_risex_ioc_request(
                env=os.environ,
                api=api,
                rpc=rpc,
                symbol=intent.symbol,
                side=intent.side,
                use_min_order_size=intent.use_min_order_size,
                slippage_bps=_DEFAULT_SLIPPAGE_BPS,
                deadline_seconds=REPLAY_DEADLINE_SECONDS,
            )

            replay_architecture = await collect_replay_protection_architecture_attestation(
                api=api,
                rpc=rpc,
                request=request,
            )
            _assert_testnet_runtime(
                network='testnet',
                chain_id=replay_architecture.chain_id,
                api_base_url=_TESTNET_API_URL,
            )

            gate = await _build_pre_order_gate(
                env=os.environ,
                api=api,
                rpc=rpc,
                disposable_account_asserted=args.disposable_account_asserted,
                dedicated_signer_asserted=args.dedicated_signer_asserted,
                replay_protection_architecture_attestation=replay_architecture,
                operatorhub_bypass_disabled=args.operatorhub_bypass_disabled,
                fund_movement_path_absent=args.fund_movement_path_absent,
            )
            freshness_probe = make_freshness_probe(
                api=api,
                rpc=rpc,
                account_address=request.permit.account_address,
                signer_address=request.permit.signer_address,
                operatorhub_bypass_disabled=args.operatorhub_bypass_disabled,
                fund_movement_path_absent=args.fund_movement_path_absent,
            )

            async with await arm_risex_signed_testnet_execution(
                env=os.environ,
                api=api,
                rpc=rpc,
                pre_order_gate=gate,  # type: ignore[arg-type]
                freshness_probe=freshness_probe,
                explicit_approval=True,
                disposable_account_asserted=args.disposable_account_asserted,
                dedicated_signer_asserted=args.dedicated_signer_asserted,
                operatorhub_bypass_disabled=args.operatorhub_bypass_disabled,
                fund_movement_path_absent=args.fund_movement_path_absent,
            ) as session:
                transport = session.adapter.transport
                if not isinstance(transport, RISExSignedTestnetHTTPTransport):
                    raise SignedTestnetBlocked(
                        'RISEx negative replay probe requires signed testnet transport'
                    )

                async def collect_consumed_nonce(**kwargs: Any) -> Any:
                    return await collect_consumed_nonce_evidence(
                        rpc,
                        authorization_address=replay_architecture.authorization_address,
                        **kwargs,
                    )

                async def validate_second_preconditions(
                    *,
                    request: Any,
                    evidence: Any,
                ) -> Any:
                    del evidence
                    return await _validate_second_submission_preconditions(
                        api=api,
                        rpc=rpc,
                        request=request,
                        replay_architecture=replay_architecture,
                    )

                result = await _execute_replay_sequence(
                    request=request,
                    transport=transport,
                    collect_consumed_nonce=collect_consumed_nonce,
                    validate_second_preconditions=validate_second_preconditions,
                )

    return sanitize_diagnostic_payload(
        {
            **result,
            'network': 'testnet',
            'symbol': intent.symbol,
            'side': intent.side,
            'market_id': request.order.market_id,
            'size_steps': request.order.size_steps,
            'price_ticks': request.order.price_ticks,
            'client_order_id': request.order.client_order_id,
            'nonce_anchor': request.permit.nonce_anchor,
            'nonce_bitmap_index': request.permit.nonce_bitmap_index,
            'deadline': request.permit.deadline,
            'action_hash': request.permit.action_hash,
        }
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.approve_two_identical_signed_order_submissions:
        print(
            json.dumps(
                {
                    'submitted': False,
                    'post_attempted': False,
                    'reason': (
                        'explicit --approve-two-identical-signed-order-submissions '
                        'flag is required'
                    ),
                },
                sort_keys=True,
            )
        )
        return 2

    try:
        payload = asyncio.run(_execute_once(args))
    except (
        ProviderDataMalformed,
        ProviderReadUnavailable,
        ProviderWriteDisabled,
        SignedTestnetBlocked,
    ) as exc:
        payload = {'submitted': False, 'error_type': type(exc).__name__}
        print(json.dumps(sanitize_diagnostic_payload(payload), sort_keys=True))
        return 2

    print(json.dumps(sanitize_diagnostic_payload(payload), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
