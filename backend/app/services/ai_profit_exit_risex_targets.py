"""Refresh RISEx TESTNET SHADOW targets using shared deterministic sizing.

This runs only as a pre-evaluation research step, with create_jobs=False.
It cannot publish a CopyJob or submit to RISEx. It reuses the same
reconcile_observed_follower sizing/risk/causal-source logic as activation.
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.ratelimit import Priority
from app.db.position_ledger_lock import position_ledger_lock
from app.models.entities import (
    CopyState, ExecutionEpoch, RISExTradingAccount, User, UserState,
)
from app.services.reconcile import (
    _positions,
    observed_master_mids,
    position_configs,
    reconcile_observed_follower,
)
from app.services.risex_reconcile_observation import read_risex_reconcile_observation

log = logging.getLogger(__name__)


async def refresh_risex_shadow_targets(
    db: AsyncSession, *, master_hl, master_snapshot,
    snapshot_started_order: int,
) -> dict:
    """Reconcile target sizes for RISEx testnet SHADOW only.

    Requires exact ENABLE_LIVE_TRADING=false per ADR-0002. An execution
    epoch must match the dedicated RISEx account, never the legacy HL
    trading account. Failed readers leave no invented targets.
    """
    summary = {"reviewed": 0, "updated": 0, "skipped": 0}
    if os.environ.get("ENABLE_LIVE_TRADING") != "false":
        return summary
    rows = (await db.execute(
        select(User, RISExTradingAccount)
        .join(RISExTradingAccount, RISExTradingAccount.user_id == User.id)
        .where(
            User.state == UserState.ACTIVE,
            User.copy_state == CopyState.SHADOW,
            User.execution_provider == "risex",
        )
        .order_by(User.id)
    )).all()
    if not rows:
        return summary
    if not isinstance(snapshot_started_order, int) or snapshot_started_order <= 0:
        return summary
    if not isinstance(master_snapshot.perp_state, dict):
        return summary
    master_equity = Decimal(str(master_snapshot.account_value))
    if not master_equity.is_finite() or master_equity <= 0:
        return summary
    # Cache one authoritative source mids snapshot per evaluation, never use
    # a RISEx price as a Hyperliquid master-source price.
    source_mids = observed_master_mids(
        await master_hl.mids(priority=Priority.MASTER_STATE),
        snapshot_started_order,
    )
    positions = _positions(master_snapshot.perp_state)
    configs = position_configs(master_snapshot.perp_state)

    for user, account in rows:
        summary["reviewed"] += 1
        if user.active_execution_epoch_id is None:
            summary["skipped"] += 1
            continue
        epoch = await db.get(ExecutionEpoch, user.active_execution_epoch_id)
        if (
            epoch is None
            or epoch.user_id != user.id
            or epoch.provider != "risex"
            or epoch.network != "testnet"
            or epoch.ended_at is not None
            or not epoch.account_address
            or epoch.account_address.lower() != account.account_address.lower()
        ):
            summary["skipped"] += 1
            continue
        try:
            async with position_ledger_lock(user.id):
                observation = await read_risex_reconcile_observation(
                    account_address=account.account_address,
                    epoch_id=epoch.id, network="testnet",
                    started_at=epoch.started_at,
                )
                result = await reconcile_observed_follower(
                    db, user, observation=observation,
                    master_positions=positions,
                    master_equity=master_equity,
                    master_mids=source_mids,
                    master_configs=configs,
                    create_jobs=False,
                )
            if result.get("status") == "OK" and result.get("jobs_created") == 0:
                summary["updated"] += 1
            else:
                summary["skipped"] += 1
        except Exception:
            await db.rollback()
            summary["skipped"] += 1
            log.warning(
                "RISEx shadow target refresh failed closed",
                extra={"user_id": str(user.id)}, exc_info=True,
            )
    return summary
