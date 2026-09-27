from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.risex_http import RISExReadOnlyHTTPTransport
from app.security.risex_consumed_nonce_evidence import (
    RISExConsumedNonceEvidence,
    collect_consumed_nonce_evidence,
)
from app.security.risex_deployment_runtime import (
    PublicAPITransport,
    PublicRPCTransport,
    RISExReadOnlyRPCTransport,
)
from app.core.logging import get_logger
from app.db.position_ledger_lock import position_ledger_lock
from app.models.entities import (
    CopyJob,
    Execution,
    ExecutionEpoch,
    ExecutionState,
    JobState,
)
from app.services.risex_copy_execution import (
    RISExSubmissionOutcome,
    _finish_case_a_job,
    settle_risex_execution_under_accounting_lock,
)
from app.services.risex_provider_truth import (
    RISExLedgerBaseline,
    RISExProviderTruthSnapshot,
    _apply_snapshot_under_settlement_lock,
    _baseline_from_ledger,
    _read_ledger,
)
from app.services.risex_risk_planning import (
    parse_risex_portfolio_details,
    parse_risex_risk_market,
)


RISEX_4C_HISTORY_LOOKBACK_SECONDS = 120
RISEX_4C_DEFAULT_MAX_HISTORY_PAGES = 10
RISEX_4C_DEFAULT_BATCH_LIMIT = 100

_RISEX_TESTNET_API_URL = "https://api.testnet.rise.trade"
_RISEX_TESTNET_RPC_URL = "https://testnet.riselabs.xyz"
_PINNED_RISEX_TESTNET_AUTHORIZATION = "0x6DA86F486b5E6536358F5b122dBe184522CA0eE3"
_ACTIVE_STATES = (ExecutionState.SUBMITTING, ExecutionState.UNKNOWN)

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _RISEx4CCandidate:
    execution_id: uuid.UUID
    job_id: uuid.UUID
    user_id: uuid.UUID
    execution_epoch_id: uuid.UUID
    asset: str
    account_address: str
    client_order_id: int
    nonce_anchor: int
    nonce_bitmap_index: int
    created_at: datetime
    baseline: RISExLedgerBaseline


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _positive_price_or_none(value: object) -> Decimal | None:
    parsed = _decimal_or_none(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _parse_decimal_string_int(value: object) -> int | None:
    if not isinstance(value, str) or not value or not value.isdecimal():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _history_lower_bound_ns(execution_created_at: datetime) -> int:
    lower = execution_created_at - timedelta(
        seconds=RISEX_4C_HISTORY_LOOKBACK_SECONDS
    )
    return int(lower.timestamp() * 1_000_000_000)


def _normalize_exact_order(
    order: Mapping[str, object],
    *,
    account: str,
    client_order_id: int,
) -> dict[str, Any] | None:
    raw_client_order_id = order.get("client_order_id")
    parsed_client_order_id = _parse_decimal_string_int(raw_client_order_id)
    if parsed_client_order_id != client_order_id:
        return None

    sender = order.get("sender")
    if not isinstance(sender, str) or sender.lower() != account.lower():
        return None

    status = order.get("status")
    if not isinstance(status, str):
        return None

    filled_size = _decimal_or_none(order.get("filled_size"))
    if filled_size is None or filled_size < 0:
        return None

    if status == "ORDER_STATUS_FILLED":
        if filled_size <= 0:
            return None
        execution_state = ExecutionState.FILLED
    elif status == "ORDER_STATUS_CANCELLED":
        execution_state = (
            ExecutionState.FILLED
            if filled_size > 0
            else ExecutionState.CANCELED
        )
    else:
        return None

    provider_order_id_raw = order.get("id")
    if isinstance(provider_order_id_raw, bool) or provider_order_id_raw is None:
        return None
    provider_order_id = str(provider_order_id_raw)
    if not provider_order_id:
        return None

    tx_hash = order.get("tx_hash")
    if not isinstance(tx_hash, str) or not tx_hash:
        return None

    order_block_number = order.get("block_number")
    if (
        order_block_number is None
        or isinstance(order_block_number, bool)
        or not isinstance(order_block_number, (str, int))
    ):
        return None

    avg_price = _positive_price_or_none(order.get("avg_price"))
    cancel_reason = order.get("cancel_reason")
    reason = (
        str(cancel_reason)
        if execution_state == ExecutionState.CANCELED
        and cancel_reason not in (None, "")
        else None
    )

    return {
        "provider_order_id": provider_order_id,
        "client_order_id": parsed_client_order_id,
        "execution_state": execution_state,
        "filled_size": filled_size,
        "avg_price": avg_price,
        "reason": reason,
        "tx_hash": tx_hash,
        "order_block_number": order_block_number,
        "status": status,
        "source": "/v1/orders",
        "raw_order": dict(order),
    }


async def read_risex_order_history(
    api: PublicAPITransport,
    *,
    account: str,
    client_order_id: int,
    execution_created_at: datetime,
    max_pages: int = RISEX_4C_DEFAULT_MAX_HISTORY_PAGES,
) -> dict[str, Any] | None:
    """Return one exact terminal order-history match, otherwise remain unresolved."""

    if (
        getattr(api, "public_read_only", False) is not True
        or type(client_order_id) is not int
        or not 0 < client_order_id < (1 << 64)
        or not isinstance(execution_created_at, datetime)
        or type(max_pages) is not int
        or max_pages <= 0
    ):
        return None

    lower_bound_ns = _history_lower_bound_ns(execution_created_at)
    exact_matches: list[Mapping[str, object]] = []

    try:
        for page_number in range(1, max_pages + 1):
            payload = await api.get_json(
                "/v1/orders",
                params={
                    "account": account,
                    "page": page_number,
                    "start_time": lower_bound_ns,
                },
            )
            data = payload.get("data")
            if not isinstance(data, Mapping):
                return None
            orders = data.get("orders")
            page = data.get("page")
            has_next_page = data.get("has_next_page")
            if (
                not isinstance(orders, list)
                or type(page) is not int
                or page != page_number
                or type(has_next_page) is not bool
            ):
                return None

            for raw_order in orders:
                if not isinstance(raw_order, Mapping):
                    return None
                raw_client_order_id = _parse_decimal_string_int(
                    raw_order.get("client_order_id")
                )
                created_at_ns = _parse_decimal_string_int(raw_order.get("created_at"))
                if raw_client_order_id is None or created_at_ns is None:
                    return None
                if created_at_ns < lower_bound_ns:
                    continue
                if raw_client_order_id == client_order_id:
                    exact_matches.append(raw_order)

            if not has_next_page:
                break
            if page_number > max_pages:
                return None
        else:
            return None
    except Exception:
        return None

    if len(exact_matches) != 1:
        return None
    return _normalize_exact_order(
        exact_matches[0],
        account=account,
        client_order_id=client_order_id,
    )


async def _collect_risex_4c_resolution_evidence(
    api: PublicAPITransport,
    rpc: PublicRPCTransport,
    *,
    authorization_address: str,
    account: str,
    nonce_anchor: int,
    nonce_bitmap_index: int,
    client_order_id: int,
    execution_created_at: datetime,
    max_pages: int = RISEX_4C_DEFAULT_MAX_HISTORY_PAGES,
) -> dict[str, Any] | None:
    """Require coherent consumed-nonce evidence before exact order-history resolution."""

    try:
        nonce_evidence = await collect_consumed_nonce_evidence(
            rpc,
            authorization_address=authorization_address,
            account=account,
            nonce_anchor=nonce_anchor,
            nonce_bitmap_index=nonce_bitmap_index,
        )
    except Exception:
        return None

    if (
        nonce_evidence.nonce_anchor != nonce_anchor
        or nonce_evidence.nonce_bitmap_index != nonce_bitmap_index
        or nonce_evidence.bitmap_consistent is not True
        or nonce_evidence.is_nonce_used is not True
    ):
        return None

    order = await read_risex_order_history(
        api,
        account=account,
        client_order_id=client_order_id,
        execution_created_at=execution_created_at,
        max_pages=max_pages,
    )
    if order is None:
        return None

    return {
        **order,
        "nonce_evidence": nonce_evidence,
    }


def _valid_epoch_account(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        return False
    try:
        return int(value[2:], 16) != 0
    except ValueError:
        return False


async def _candidate_ids(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    batch_limit: int,
) -> list[uuid.UUID]:
    query = (
        select(Execution.id)
        .where(
            Execution.execution_provider == "risex",
            Execution.state.in_(_ACTIVE_STATES),
        )
        .order_by(Execution.created_at, Execution.id)
        .limit(batch_limit)
    )
    if user_id is not None:
        query = query.where(Execution.user_id == user_id)
    ids = list((await db.execute(query)).scalars().all())
    await db.commit()
    return ids


async def _load_candidate_baseline(
    db: AsyncSession,
    execution_id: uuid.UUID,
) -> _RISEx4CCandidate | None:
    execution = (
        await db.execute(
            select(Execution)
            .where(Execution.id == execution_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        execution is None
        or execution.state not in _ACTIVE_STATES
        or execution.execution_provider != "risex"
        or execution.execution_network != "testnet"
        or execution.execution_epoch_id is None
    ):
        await db.commit()
        return None

    job = (
        await db.execute(
            select(CopyJob)
            .where(CopyJob.id == execution.copy_job_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    epoch = (
        await db.execute(
            select(ExecutionEpoch)
            .where(ExecutionEpoch.id == execution.execution_epoch_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if (
        job is None
        or epoch is None
        or job.user_id != execution.user_id
        or job.execution_epoch_id != execution.execution_epoch_id
        or job.execution_provider != "risex"
        or job.execution_network != "testnet"
        or epoch.user_id != execution.user_id
        or epoch.provider != "risex"
        or epoch.network != "testnet"
        or not _valid_epoch_account(epoch.account_address)
    ):
        await db.commit()
        return None

    client_order_id = (
        int(execution.client_order_id)
        if execution.client_order_id is not None
        else 0
    )
    if (
        not 0 < client_order_id < (1 << 64)
        or execution.client_order_id is None
        or Decimal(client_order_id) != execution.client_order_id
        or type(execution.nonce_anchor) is not int
        or type(execution.nonce_bitmap_index) is not int
        or not 0 <= execution.nonce_anchor < (1 << 48)
        or not 0 <= execution.nonce_bitmap_index < (1 << 8)
    ):
        await db.commit()
        return None

    ledger = await _read_ledger(
        db,
        user_id=execution.user_id,
        asset=execution.asset,
        for_update=False,
    )
    candidate = _RISEx4CCandidate(
        execution_id=execution.id,
        job_id=execution.copy_job_id,
        user_id=execution.user_id,
        execution_epoch_id=execution.execution_epoch_id,
        asset=execution.asset,
        account_address=str(epoch.account_address),
        client_order_id=client_order_id,
        nonce_anchor=execution.nonce_anchor,
        nonce_bitmap_index=execution.nonce_bitmap_index,
        created_at=execution.created_at,
        baseline=_baseline_from_ledger(ledger),
    )
    await db.commit()
    return candidate


async def _read_absolute_snapshot(
    api: PublicAPITransport,
    candidate: _RISEx4CCandidate,
) -> RISExProviderTruthSnapshot | None:
    try:
        markets_payload = await api.get_json("/v1/markets")
        portfolio_payload = await api.get_json(
            "/v1/portfolio/details",
            params={"account": candidate.account_address},
        )
        market = parse_risex_risk_market(markets_payload, symbol=candidate.asset)
        portfolio = parse_risex_portfolio_details(portfolio_payload)
        positions = [
            position
            for position in portfolio.positions
            if position.market_id == market.market_id
        ]
        if len(positions) > 1:
            return None
        if positions:
            position_size = positions[0].size
            mark_price = positions[0].mark_price
        else:
            position_size = Decimal(0)
            mark_price = market.mark_price
        if mark_price is None or mark_price <= 0:
            return None
        return RISExProviderTruthSnapshot(
            market_id=market.market_id,
            position_size=position_size,
            mark_price=mark_price,
            verified_at=datetime.now(UTC),
        )
    except Exception:
        return None


async def _reload_after_rollback(
    db: AsyncSession,
    candidate: _RISEx4CCandidate,
) -> tuple[Execution | None, CopyJob | None]:
    await db.rollback()
    execution = (
        await db.execute(
            select(Execution)
            .where(Execution.id == candidate.execution_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    job = (
        await db.execute(
            select(CopyJob)
            .where(CopyJob.id == candidate.job_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return execution, job


def _candidate_identity_matches(
    execution: Execution,
    job: CopyJob,
    epoch: ExecutionEpoch,
    candidate: _RISEx4CCandidate,
) -> bool:
    current_client_order_id = (
        int(execution.client_order_id)
        if execution.client_order_id is not None
        else 0
    )
    return (
        execution.id == candidate.execution_id
        and execution.state in _ACTIVE_STATES
        and execution.execution_provider == "risex"
        and execution.execution_network == "testnet"
        and execution.execution_epoch_id == candidate.execution_epoch_id
        and execution.user_id == candidate.user_id
        and execution.asset == candidate.asset
        and current_client_order_id == candidate.client_order_id
        and execution.nonce_anchor == candidate.nonce_anchor
        and execution.nonce_bitmap_index == candidate.nonce_bitmap_index
        and job.id == candidate.job_id
        and job.user_id == candidate.user_id
        and job.execution_epoch_id == candidate.execution_epoch_id
        and job.execution_provider == "risex"
        and job.execution_network == "testnet"
        and epoch.id == candidate.execution_epoch_id
        and epoch.user_id == candidate.user_id
        and epoch.provider == "risex"
        and epoch.network == "testnet"
        and isinstance(epoch.account_address, str)
        and epoch.account_address.lower() == candidate.account_address.lower()
    )


async def _settle_candidate(
    db: AsyncSession,
    *,
    candidate: _RISEx4CCandidate,
    evidence: Mapping[str, Any],
    snapshot: RISExProviderTruthSnapshot,
) -> bool:
    async with position_ledger_lock(candidate.user_id):
        current_execution = (
            await db.execute(
                select(Execution)
                .where(Execution.id == candidate.execution_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        current_job = (
            await db.execute(
                select(CopyJob)
                .where(CopyJob.id == candidate.job_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        current_epoch = (
            await db.execute(
                select(ExecutionEpoch)
                .where(ExecutionEpoch.id == candidate.execution_epoch_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            current_execution is None
            or current_job is None
            or current_epoch is None
            or not _candidate_identity_matches(
                current_execution,
                current_job,
                current_epoch,
                candidate,
            )
        ):
            await _reload_after_rollback(db, candidate)
            return False

        current_ledger = await _read_ledger(
            db,
            user_id=candidate.user_id,
            asset=candidate.asset,
            for_update=True,
        )
        if _baseline_from_ledger(current_ledger) != candidate.baseline:
            await _reload_after_rollback(db, candidate)
            return False

        peer = (
            await db.execute(
                select(Execution.id)
                .where(
                    Execution.user_id == candidate.user_id,
                    Execution.asset == candidate.asset,
                    Execution.id != candidate.execution_id,
                    Execution.execution_provider == "risex",
                    Execution.state.in_(_ACTIVE_STATES),
                )
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if peer is not None:
            await _reload_after_rollback(db, candidate)
            return False

        state = evidence.get("execution_state")
        filled_size = evidence.get("filled_size")
        provider_order_id = evidence.get("provider_order_id")
        nonce_evidence = evidence.get("nonce_evidence")
        if (
            state not in {ExecutionState.FILLED, ExecutionState.CANCELED}
            or not isinstance(filled_size, Decimal)
            or filled_size < 0
            or not isinstance(provider_order_id, str)
            or not isinstance(nonce_evidence, RISExConsumedNonceEvidence)
        ):
            await _reload_after_rollback(db, candidate)
            return False

        outcome = RISExSubmissionOutcome(
            definitive=True,
            execution_state=state,
            reservation_active=False,
            provider_order_id=provider_order_id,
            filled_quantity=filled_size,
            reason=(
                str(evidence.get("reason"))
                if evidence.get("reason") not in (None, "")
                else None
            ),
        )

        async def persist_snapshot(
            settlement_db: AsyncSession,
            locked_execution: Execution,
        ) -> Mapping[str, Any]:
            result = await _apply_snapshot_under_settlement_lock(
                settlement_db,
                locked_execution,
                snapshot,
            )
            avg_price = evidence.get("avg_price")
            locked_execution.avg_price = (
                avg_price if isinstance(avg_price, Decimal) else None
            )
            response = dict(locked_execution.response or {})
            response["risex_4c"] = {
                "nonce_block_number": nonce_evidence.block_number,
                "tx_hash": evidence.get("tx_hash"),
                "order_block_number": evidence.get("order_block_number"),
                "status": evidence.get("status"),
                "source": evidence.get("source"),
            }
            locked_execution.response = response
            await settlement_db.flush()
            return result

        settled = await settle_risex_execution_under_accounting_lock(
            db,
            execution_id=candidate.execution_id,
            outcome=outcome,
            persist_provider_truth=persist_snapshot,
        )
        fresh_job = (
            await db.execute(
                select(CopyJob)
                .where(CopyJob.id == candidate.job_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if fresh_job is None:
            await db.rollback()
            return False
        if fresh_job.state in {JobState.QUEUED, JobState.RETRYING}:
            fresh_job.state = JobState.PROCESSING
        elif fresh_job.state != JobState.PROCESSING:
            await db.rollback()
            return False
        await _finish_case_a_job(db, fresh_job, settled)
        return True


async def _close_owned_transport(transport: object) -> None:
    close = getattr(transport, "aclose", None)
    if callable(close):
        result = close()
        if hasattr(result, "__await__"):
            await result


async def resolve_risex_ambiguous_executions(
    db: AsyncSession,
    *,
    api: PublicAPITransport | None = None,
    rpc: PublicRPCTransport | None = None,
    authorization_address: str | None = None,
    user_id: uuid.UUID | None = None,
    max_history_pages: int = RISEX_4C_DEFAULT_MAX_HISTORY_PAGES,
    batch_limit: int = RISEX_4C_DEFAULT_BATCH_LIMIT,
) -> dict[str, int]:
    """Resolve RISEx SUBMITTING/UNKNOWN executions without any provider write path."""

    if (
        type(max_history_pages) is not int
        or max_history_pages <= 0
        or type(batch_limit) is not int
        or batch_limit <= 0
    ):
        return {
            "candidates": 0,
            "resolved": 0,
            "unresolved": 0,
            "skipped": 0,
        }

    ids = await _candidate_ids(db, user_id=user_id, batch_limit=batch_limit)
    result = {
        "candidates": len(ids),
        "resolved": 0,
        "unresolved": 0,
        "skipped": 0,
    }
    if not ids:
        return result

    owned_api = api is None
    owned_rpc = rpc is None
    if api is None:
        api = RISExReadOnlyHTTPTransport(base_url=_RISEX_TESTNET_API_URL)
    if rpc is None:
        rpc = RISExReadOnlyRPCTransport(rpc_url=_RISEX_TESTNET_RPC_URL)
    auth_address = authorization_address or _PINNED_RISEX_TESTNET_AUTHORIZATION

    try:
        for execution_id in ids:
            try:
                candidate = await _load_candidate_baseline(db, execution_id)
                if candidate is None:
                    result["skipped"] += 1
                    continue

                evidence = await _collect_risex_4c_resolution_evidence(
                    api,
                    rpc,
                    authorization_address=auth_address,
                    account=candidate.account_address,
                    nonce_anchor=candidate.nonce_anchor,
                    nonce_bitmap_index=candidate.nonce_bitmap_index,
                    client_order_id=candidate.client_order_id,
                    execution_created_at=candidate.created_at,
                    max_pages=max_history_pages,
                )
                if evidence is None:
                    result["unresolved"] += 1
                    continue

                snapshot = await _read_absolute_snapshot(api, candidate)
                if snapshot is None:
                    result["unresolved"] += 1
                    continue

                if await _settle_candidate(
                    db,
                    candidate=candidate,
                    evidence=evidence,
                    snapshot=snapshot,
                ):
                    result["resolved"] += 1
                else:
                    result["unresolved"] += 1
            except Exception as exc:
                await db.rollback()
                log.warning(
                    "RISEx 4C candidate resolution failed",
                    extra={
                        "execution_id": str(execution_id),
                        "error_type": type(exc).__name__,
                    },
                    exc_info=True,
                )
                result["unresolved"] += 1
                continue
    finally:
        if owned_api:
            await _close_owned_transport(api)
        if owned_rpc:
            await _close_owned_transport(rpc)

    return result
