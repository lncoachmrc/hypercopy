"""Sampled profit-retention context for AI Profit Exit SHADOW research only.

The peak is the maximum of durable past *evaluation* samples and current PnL;
this is NOT exchange-wide MFE and cannot prove a better trading strategy.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import AIProfitExitDecision

RETENTION_VARIANT = "profit_retention_shadow_v1"


def build_retention_context(
    *,
    current_net_pnl: Decimal,
    prior_peak_net_pnl: Decimal | None,
    prior_latest_net_pnl: Decimal | None,
    prior_samples: int,
    pnl_complete: bool,
) -> dict:
    """Describe observed profit erosion without an automatic exit threshold."""
    current = Decimal(str(current_net_pnl))
    if not current.is_finite() or current <= 0:
        raise ValueError("Profit retention requires strictly positive finite net PnL")
    if prior_samples < 0:
        raise ValueError("Negative historical sample count")
    if prior_samples == 0:
        if prior_peak_net_pnl is not None or prior_latest_net_pnl is not None:
            raise ValueError("Unexpected history without samples")
        peak = current
        latest = None
    else:
        if prior_peak_net_pnl is None or prior_latest_net_pnl is None:
            raise ValueError("Historical PnL is incomplete")
        historic_peak = Decimal(str(prior_peak_net_pnl))
        latest = Decimal(str(prior_latest_net_pnl))
        if not (historic_peak.is_finite() and latest.is_finite()):
            raise ValueError("Non-finite historical PnL")
        if historic_peak <= 0 or latest <= 0 or historic_peak < latest:
            raise ValueError("Invalid historical PnL sequence")
        peak = max(historic_peak, current)

    giveback = peak - current
    percent = giveback * Decimal(100) / peak
    return {
        "variant": RETENTION_VARIANT,
        "status": "observed",
        "comparison_available": prior_samples > 0,
        "sample_count": prior_samples + 1,
        "current_estimated_net_pnl_usdc": str(current),
        "observed_peak_net_pnl_usdc": str(peak),
        "observed_giveback_usdc": str(giveback),
        "observed_giveback_pct": str(percent.quantize(Decimal("0.01"))),
        "previous_net_pnl_usdc": str(latest) if latest is not None else None,
        "change_since_previous_usdc": str(current - latest) if latest is not None else None,
        "sample_scope": "same_user_epoch_network_asset_cycle_version_position_and_completeness",
        "peak_is_intraperiod_mfe": False,
        "funding_complete": pnl_complete,
    }


async def read_retention_context(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    execution_epoch_id: uuid.UUID,
    execution_provider: str,
    execution_network: str,
    asset: str,
    source_cycle_id: str,
    state_version: int,
    follower_position_size: Decimal,
    pnl_complete: bool,
    current_net_pnl: Decimal,
    evaluation_basis: str | None = None,
    funding_model: str | None = None,
) -> dict:
    """Compute a durable high-water mark in one indexed SQL query.

    Restrict the baseline to an identical, source-verified position state.
    A source scale-in, reversal, new destination or changed follower size
    automatically starts a new comparable series.
    """
    stmt = (
        select(
            AIProfitExitDecision.net_pnl,
            func.max(AIProfitExitDecision.net_pnl).over().label("peak"),
            func.count(AIProfitExitDecision.id).over().label("observations"),
        )
        .where(
            AIProfitExitDecision.user_id == user_id,
            AIProfitExitDecision.execution_epoch_id == execution_epoch_id,
            AIProfitExitDecision.execution_provider == execution_provider,
            AIProfitExitDecision.execution_network == execution_network,
            AIProfitExitDecision.asset == asset,
            AIProfitExitDecision.source_cycle_id == source_cycle_id,
            AIProfitExitDecision.state_version == state_version,
            AIProfitExitDecision.follower_position_size == follower_position_size,
            AIProfitExitDecision.pnl_complete.is_(pnl_complete),
            AIProfitExitDecision.net_pnl > 0,
        )
        .order_by(
            AIProfitExitDecision.decided_at.desc(),
            AIProfitExitDecision.id.desc(),
        )
        .limit(1)
    )
    # Optional stricter scope for a cross-venue estimated cost model. Existing
    # callers keep their original behavior; RISEx cannot mix changed fee assumptions.
    if evaluation_basis is not None:
        stmt = stmt.where(AIProfitExitDecision.decision_inputs['evaluation_basis'].as_string() == evaluation_basis)
    if funding_model is not None:
        stmt = stmt.where(AIProfitExitDecision.decision_inputs['funding_model'].as_string() == funding_model)
    row = (await db.execute(stmt)).one_or_none()
    return build_retention_context(
        current_net_pnl=current_net_pnl,
        prior_latest_net_pnl=row[0] if row else None,
        prior_peak_net_pnl=row[1] if row else None,
        prior_samples=int(row[2]) if row else 0,
        pnl_complete=pnl_complete,
    )
