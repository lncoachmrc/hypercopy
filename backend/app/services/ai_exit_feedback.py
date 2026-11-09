"""Causal, versioned SHADOW feedback. No orders, model training or promotion.

Compare the estimated residual PnL at a decision with the first comparable
sample 30--35 minutes later. This is NOT realized or final-cycle profit.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Iterable, Mapping

FEEDBACK_VERSION = 'exit_feedback_30m_v1'
MIN_CYCLES = 20
MAX_CYCLES = 100


def _number(raw) -> Decimal:
    if isinstance(raw, bool):
        raise ValueError('boolean is not a price')
    value = Decimal(str(raw))
    if not value.is_finite():
        raise ValueError('nonfinite economic evidence')
    return value


def summarize_feedback(rows: Iterable[Mapping], *, as_of: datetime, current_cycle: str) -> dict:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError('as_of must be timezone-aware')
    accepted = []
    rejected = 0
    for row in rows:
        try:
            cycle = row['source_cycle_id']
            decided, observed = row['decided_at'], row['observed_at']
            if not isinstance(cycle, str) or not cycle or cycle == current_cycle:
                raise ValueError('current or missing cycle')
            if not isinstance(decided, datetime) or not isinstance(observed, datetime):
                raise ValueError('outcome not observed')
            if decided.tzinfo is None or observed.tzinfo is None:
                raise ValueError('timestamps need timezone')
            delay = (observed - decided).total_seconds()
            if not 1800 <= delay <= 2100 or observed >= as_of:
                raise ValueError('outcome outside causal horizon')
            before, after = _number(row['before_pnl']), _number(row['after_pnl'])
            position, entry = _number(row['position']), _number(row['entry'])
            if before <= 0 or position == 0 or entry <= 0 or _number(row['outcome_entry']) != entry:
                raise ValueError('incomparable position cost basis')
            advantage = (before - after) / (abs(position) * entry) * Decimal(10000)
            accepted.append((cycle, decided, str(row['decision_id']), observed, advantage))
        except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
            rejected += 1
    # Earliest eligible decision per source cycle; do not count each minute as
    # an independent trade. Sorting makes the evidence independent of row order.
    unique = {}
    for item in sorted(accepted, key=lambda r: (r[0], r[1], r[2])):
        unique.setdefault(item[0], item)
    evidence = sorted(unique.values(), key=lambda r: (r[1], r[0], r[2]), reverse=True)[:MAX_CYCLES]
    deltas = [row[4] for row in evidence]
    wins = sum(d > 0 for d in deltas)
    losses = sum(d < 0 for d in deltas)
    count = len(evidence)
    mean = sum(deltas, Decimal(0)) / count if count else None
    posterior = Decimal(wins + 1) / Decimal(wins + losses + 2)
    canonical = [[r[0], r[2], r[1].isoformat(), r[3].isoformat(), str(r[4])] for r in evidence]
    digest = hashlib.sha256(json.dumps([FEEDBACK_VERSION, canonical], separators=(',', ':')).encode()).hexdigest()
    return {
        'version': FEEDBACK_VERSION,
        'status': 'observed' if count >= MIN_CYCLES else 'warming_up',
        'sample_count': count,
        'excluded_rows': rejected,
        'minimum_cycles': MIN_CYCLES,
        'horizon_seconds': 1800,
        'max_outcome_delay_seconds': 2100,
        'outcome_kind': 'sampled_30m_counterfactual',
        'mean_close_advantage_bps': str(mean) if mean is not None else None,
        'close_better_count': wins,
        'hold_better_count': losses,
        'tie_count': count - wins - losses,
        'close_advantage_posterior_mean': str(posterior),
        'evidence_hash': digest,
        'as_of': as_of.astimezone(UTC).isoformat(),
        'promotion_allowed': False,
        'realized_performance': False,
    }


async def read_exit_feedback(db, *, user_id, epoch_id, provider: str, network: str,
                             asset: str, side: str, basis: str, pnl_complete: bool, funding_model: str,
                             current_cycle: str, as_of: datetime) -> dict:
    from sqlalchemy import text

    # Only versions that explicitly persisted SHADOW provenance are learnable.
    # No implicit promotion of legacy, ON, another venue or real-capital data.
    stmt = text("""
        WITH first_decisions AS (
            SELECT DISTINCT ON (d.source_cycle_id) d.*
            FROM ai_profit_exit_decisions d
            WHERE d.user_id = :user_id AND d.execution_epoch_id = :epoch_id
              AND d.execution_provider = :provider AND d.execution_network = :network
              AND d.asset = :asset AND d.side = :side
              AND d.source_cycle_id <> :current_cycle
              AND d.decided_at <= :mature_before
              AND d.copy_job_id IS NULL AND d.intent_state IS NULL
              AND d.action IN ('HOLD', 'CLOSE_PROFIT') AND d.net_pnl > 0
              AND d.pnl_complete = :pnl_complete
              AND d.decision_inputs->>'decision_mode' = 'SHADOW'
              AND d.decision_inputs->>'evaluation_basis' = :basis
              AND d.decision_inputs->>'funding_model' = :funding_model
              AND d.decision_inputs->>'learning_policy_version' = :version
            ORDER BY d.source_cycle_id, d.decided_at, d.id
        ), recent AS (
            SELECT * FROM first_decisions ORDER BY decided_at DESC, id DESC LIMIT 100
        )
        SELECT d.id AS decision_id, d.source_cycle_id, d.decided_at,
               d.net_pnl AS before_pnl, d.follower_position_size AS position,
               d.decision_inputs->>'entry_price' AS entry,
               o.net_pnl AS after_pnl, o.entry_price AS outcome_entry, o.observed_at
        FROM recent d
        LEFT JOIN LATERAL (
            SELECT o.net_pnl, o.entry_price, o.observed_at
            FROM ai_profit_exit_observations o
            WHERE o.user_id = d.user_id AND o.execution_epoch_id = d.execution_epoch_id
              AND o.execution_provider = d.execution_provider
              AND o.execution_network = d.execution_network AND o.asset = d.asset
              AND o.source_cycle_id = d.source_cycle_id AND o.state_version = d.state_version
              AND o.follower_position_size = d.follower_position_size
              AND o.pnl_complete = d.pnl_complete AND o.evaluation_basis = :basis
              AND o.funding_model = d.decision_inputs->>'funding_model'
              AND o.complete IS TRUE AND o.position_matches IS TRUE
              AND o.net_pnl IS NOT NULL
              AND o.observed_at >= d.decided_at + interval '30 minutes'
              AND o.observed_at <= d.decided_at + interval '35 minutes'
              AND o.observed_at < :as_of
            ORDER BY o.observed_at, o.evaluation_slot, o.id LIMIT 1
        ) o ON TRUE
        ORDER BY d.decided_at, d.id
    """)
    result = await db.execute(stmt, {
        'user_id': user_id, 'epoch_id': epoch_id, 'provider': provider,
        'network': network, 'asset': asset, 'side': side, 'basis': basis,
        'pnl_complete': pnl_complete, 'funding_model': funding_model, 'current_cycle': current_cycle,
        'mature_before': as_of - timedelta(minutes=30), 'as_of': as_of,
        'version': FEEDBACK_VERSION,
    })
    summary = summarize_feedback(result.mappings().all(), as_of=as_of, current_cycle=current_cycle)
    return {**summary, 'provider': provider, 'network': network, 'basis': basis,
            'pnl_complete': pnl_complete}
