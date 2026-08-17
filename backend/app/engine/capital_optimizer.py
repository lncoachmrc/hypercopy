from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from app.engine.sizing import EXCHANGE_MIN_NOTIONAL

ZERO = Decimal('0')
ONE = Decimal('1')
HUNDRED = Decimal('100')


def _d(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _coverage(entries: list[dict], selected: set[str]) -> Decimal:
    total = sum((x['master_notional'] for x in entries), ZERO)
    if total <= 0:
        return ZERO
    covered = sum((x['master_notional'] for x in entries if x['asset'] in selected), ZERO)
    return covered / total * HUNDRED


def _tracking_error(exact: Mapping[str, Decimal], candidate: Mapping[str, Decimal]) -> Decimal:
    gross = sum((abs(v) for v in exact.values()), ZERO)
    if gross <= 0:
        return ZERO
    error = sum((abs(candidate.get(asset, ZERO) - value) for asset, value in exact.items()), ZERO)
    return error / gross * HUNDRED


def _base_entries(
    master_positions: Mapping[str, Decimal],
    master_mids: Mapping[str, str | Decimal],
    master_equity: Decimal,
    multiplier: Decimal,
    persistence: Mapping[str, float | Decimal] | None,
) -> list[dict]:
    if master_equity <= 0:
        return []
    out: list[dict] = []
    for asset, raw_size in master_positions.items():
        size = _d(raw_size)
        mark = _d(master_mids.get(asset, '0') or '0')
        if size == 0 or mark <= 0:
            continue
        notional = abs(size) * mark
        sign = ONE if size > 0 else Decimal('-1')
        signed_equity_weight = sign * (notional / master_equity) * multiplier
        p = _d((persistence or {}).get(asset, Decimal('0.5')))
        p = max(ZERO, min(ONE, p))
        out.append({
            'asset': asset,
            'master_notional': notional,
            'signed_equity_weight': signed_equity_weight,
            'persistence': p,
        })
    return out


def _exact_candidate(entries: list[dict], follower_equity: Decimal, floor: Decimal) -> dict:
    weights = {x['asset']: x['signed_equity_weight'] for x in entries}
    targets = {asset: weight * follower_equity for asset, weight in weights.items()}
    selected = {asset for asset, value in targets.items() if abs(value) >= floor}
    return {
        'id': 'exact',
        'label': 'Exact Ratio',
        'buffer_pct': '0',
        'allocation_scale': '1',
        'coverage_pct': str(_coverage(entries, selected).quantize(Decimal('0.01'))),
        'tracking_error_pct': '0.00',
        'selected_assets': sorted(selected),
        'signed_equity_weights': {k: str(v) for k, v in weights.items()},
    }


def _compressed_candidate(entries: list[dict], follower_equity: Decimal, floor: Decimal, *, candidate_id: str, label: str, buffer_pct: Decimal) -> dict:
    if follower_equity <= 0 or not entries:
        return {
            'id': candidate_id, 'label': label, 'buffer_pct': str(buffer_pct),
            'allocation_scale': '0',
            'coverage_pct': '0.00', 'tracking_error_pct': '100.00',
            'selected_assets': [], 'signed_equity_weights': {},
        }

    base_weights = {x['asset']: x['signed_equity_weight'] for x in entries}
    exact_targets = {asset: weight * follower_equity for asset, weight in base_weights.items()}
    gross_ratio = sum((abs(x['signed_equity_weight']) for x in entries), ZERO)
    gross_budget = follower_equity * gross_ratio * (ONE - buffer_pct / HUNDRED)
    selected = list(entries)

    while selected and gross_budget > 0:
        selected_abs = sum((abs(x['signed_equity_weight']) for x in selected), ZERO)
        if selected_abs <= 0:
            selected = []
            break
        allocations = {
            x['asset']: gross_budget * abs(x['signed_equity_weight']) / selected_abs
            for x in selected
        }
        under = [x for x in selected if allocations[x['asset']] < floor]
        if not under:
            break
        # Remove the least important sub-minimum leg. Persistence is used only
        # as a tie-breaker; it never changes the master-derived weight itself.
        loser = min(
            under,
            key=lambda x: (allocations[x['asset']], x['persistence'], x['master_notional']),
        )
        selected = [x for x in selected if x['asset'] != loser['asset']]

    candidate_weights: dict[str, Decimal] = {}
    allocation_scale = ZERO
    if selected:
        selected_abs = sum((abs(x['signed_equity_weight']) for x in selected), ZERO)
        if selected_abs > 0:
            allocation_scale = gross_ratio * (ONE - buffer_pct / HUNDRED) / selected_abs
        for x in selected:
            candidate_weights[x['asset']] = x['signed_equity_weight'] * allocation_scale

    candidate_targets = {asset: weight * follower_equity for asset, weight in candidate_weights.items()}
    selected_assets = set(candidate_weights)
    return {
        'id': candidate_id,
        'label': label,
        'buffer_pct': str(buffer_pct),
        # Structural policy: at execution time current master exposure is
        # multiplied by this scale. Therefore closes, reversals and scale-ins
        # remain responsive even between LLM refreshes.
        'allocation_scale': str(allocation_scale),
        'coverage_pct': str(_coverage(entries, selected_assets).quantize(Decimal('0.01'))),
        'tracking_error_pct': str(_tracking_error(exact_targets, candidate_targets).quantize(Decimal('0.01'))),
        'selected_assets': sorted(selected_assets),
        'signed_equity_weights': {k: str(v) for k, v in candidate_weights.items()},
    }


def build_capital_candidates(
    *,
    master_positions: Mapping[str, Decimal],
    master_mids: Mapping[str, str | Decimal],
    master_equity: Decimal,
    follower_equity: Decimal,
    multiplier: Decimal = ONE,
    min_notional: Decimal = EXCHANGE_MIN_NOTIONAL,
    persistence: Mapping[str, float | Decimal] | None = None,
) -> list[dict]:
    floor = max(_d(min_notional), EXCHANGE_MIN_NOTIONAL)
    entries = _base_entries(master_positions, master_mids, _d(master_equity), _d(multiplier), persistence)
    equity = max(_d(follower_equity), ZERO)
    return [
        _exact_candidate(entries, equity, floor),
        _compressed_candidate(entries, equity, floor, candidate_id='smart_fidelity', label='Smart Fidelity', buffer_pct=Decimal('5')),
        _compressed_candidate(entries, equity, floor, candidate_id='smart_balanced', label='Smart Balanced', buffer_pct=Decimal('10')),
        _compressed_candidate(entries, equity, floor, candidate_id='smart_defensive', label='Smart Defensive', buffer_pct=Decimal('15')),
    ]


def recommended_capital_for_coverage(
    *,
    master_positions: Mapping[str, Decimal],
    master_mids: Mapping[str, str | Decimal],
    master_equity: Decimal,
    multiplier: Decimal = ONE,
    min_notional: Decimal = EXCHANGE_MIN_NOTIONAL,
    target_coverage_pct: Decimal = Decimal('90'),
) -> Decimal:
    entries = _base_entries(master_positions, master_mids, _d(master_equity), _d(multiplier), None)
    total = sum((x['master_notional'] for x in entries), ZERO)
    if total <= 0:
        return ZERO
    floor = max(_d(min_notional), EXCHANGE_MIN_NOTIONAL)
    target = max(ZERO, min(HUNDRED, _d(target_coverage_pct)))
    thresholds = []
    for x in entries:
        weight = abs(x['signed_equity_weight'])
        if weight <= 0:
            continue
        thresholds.append((floor / weight, x['master_notional']))
    thresholds.sort(key=lambda row: row[0])
    covered = ZERO
    for required_equity, notional in thresholds:
        covered += notional
        if covered / total * HUNDRED >= target:
            return required_equity.quantize(Decimal('0.01'))
    return max((row[0] for row in thresholds), default=ZERO).quantize(Decimal('0.01'))


def choose_deterministic_candidate(candidates: list[dict], target_coverage_pct: Decimal = Decimal('90')) -> dict:
    if not candidates:
        raise ValueError('No capital candidates')
    target = _d(target_coverage_pct)
    exact = next((x for x in candidates if x['id'] == 'exact'), candidates[0])
    if _d(exact['coverage_pct']) >= target:
        return exact
    eligible = [x for x in candidates if x['id'] != 'exact' and x.get('selected_assets')]
    if not eligible:
        return exact
    # Prefer candidates that reach the requested coverage; among them minimize
    # tracking error. Otherwise maximize coverage and then minimize error.
    reaching = [x for x in eligible if _d(x['coverage_pct']) >= target]
    pool = reaching or eligible
    return min(
        pool,
        key=lambda x: (
            _d(x['tracking_error_pct']) if reaching else -_d(x['coverage_pct']),
            _d(x['tracking_error_pct']),
        ),
    )


def live_policy_weight(
    *,
    policy: Mapping,
    asset: str,
    master_position: Decimal,
    master_mark: Decimal,
    master_equity: Decimal,
    multiplier: Decimal,
) -> Decimal:
    """Apply a structural AI policy to the *current* master state.

    The LLM-selected policy never freezes a position direction or notional.
    Current master flat => zero immediately; reversals flip immediately; scale
    changes flow through the current master exposure ratio. Smart policies only
    decide whether the asset participates and the bounded allocation scale.
    """
    position = _d(master_position)
    mark = _d(master_mark)
    equity = _d(master_equity)
    mult = _d(multiplier)
    if position == 0 or mark <= 0 or equity <= 0:
        return ZERO
    signed_exact = (ONE if position > 0 else Decimal('-1')) * (abs(position) * mark / equity) * mult
    if str(policy.get('candidate_id') or '') == 'exact':
        return signed_exact
    selected = set(policy.get('selected_assets') or [])
    if asset not in selected:
        return ZERO
    scale = max(ZERO, _d(policy.get('allocation_scale') or '0'))
    return signed_exact * scale
