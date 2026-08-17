from decimal import Decimal

from app.engine.capital_optimizer import (
    build_capital_candidates,
    choose_deterministic_candidate,
    live_policy_weight,
    recommended_capital_for_coverage,
)


def test_recommended_capital_tracks_weighted_strategy_coverage():
    required = recommended_capital_for_coverage(
        master_positions={
            'A': Decimal('500'),
            'B': Decimal('300'),
            'C': Decimal('50'),
            'D': Decimal('10'),
        },
        master_mids={'A': '1', 'B': '1', 'C': '1', 'D': '1'},
        master_equity=Decimal('1000'),
        min_notional=Decimal('10'),
        target_coverage_pct=Decimal('90'),
    )
    assert required == Decimal('33.33')


def test_smart_candidates_only_allocate_executable_legs_and_preserve_budget():
    equity = Decimal('40')
    candidates = build_capital_candidates(
        master_positions={
            'A': Decimal('500'),
            'B': Decimal('300'),
            'C': Decimal('50'),
            'D': Decimal('10'),
        },
        master_mids={'A': '1', 'B': '1', 'C': '1', 'D': '1'},
        master_equity=Decimal('1000'),
        follower_equity=equity,
        min_notional=Decimal('10'),
        persistence={'A': 0.9, 'B': 0.8, 'C': 0.3, 'D': 0.1},
    )
    exact = next(x for x in candidates if x['id'] == 'exact')
    smart = next(x for x in candidates if x['id'] == 'smart_balanced')

    assert Decimal(exact['coverage_pct']) < Decimal('100')
    assert smart['selected_assets']
    for value in smart['signed_equity_weights'].values():
        assert abs(Decimal(value) * equity) >= Decimal('10')

    exact_gross = sum(abs(Decimal(v)) * equity for v in exact['signed_equity_weights'].values())
    smart_gross = sum(abs(Decimal(v)) * equity for v in smart['signed_equity_weights'].values())
    assert smart_gross <= exact_gross


def test_live_policy_uses_current_master_direction_and_closes_immediately():
    policy = {
        'candidate_id': 'smart_balanced',
        'selected_assets': ['BTC'],
        'allocation_scale': '1.2',
    }
    long_weight = live_policy_weight(
        policy=policy, asset='BTC', master_position=Decimal('10'),
        master_mark=Decimal('2'), master_equity=Decimal('100'), multiplier=Decimal('1'),
    )
    flat_weight = live_policy_weight(
        policy=policy, asset='BTC', master_position=Decimal('0'),
        master_mark=Decimal('2'), master_equity=Decimal('100'), multiplier=Decimal('1'),
    )
    short_weight = live_policy_weight(
        policy=policy, asset='BTC', master_position=Decimal('-10'),
        master_mark=Decimal('2'), master_equity=Decimal('100'), multiplier=Decimal('1'),
    )
    excluded = live_policy_weight(
        policy=policy, asset='ETH', master_position=Decimal('10'),
        master_mark=Decimal('2'), master_equity=Decimal('100'), multiplier=Decimal('1'),
    )

    assert long_weight == Decimal('0.24')
    assert flat_weight == 0
    assert short_weight == Decimal('-0.24')
    assert excluded == 0


def test_new_asset_uses_exact_on_same_network_and_waits_on_split_network():
    base_policy = {
        'candidate_id': 'smart_balanced',
        'selected_assets': ['BTC'],
        'allocation_scale': '1.2',
        'known_master_assets': ['BTC', 'UNAV'],
        'follower_available_assets': ['BTC'],
    }
    same_network = {**base_policy, 'master_network': 'mainnet', 'follower_network': 'mainnet'}
    split_network = {**base_policy, 'master_network': 'mainnet', 'follower_network': 'testnet'}

    same_new_asset = live_policy_weight(
        policy=same_network, asset='ETH', master_position=Decimal('10'),
        master_mark=Decimal('2'), master_equity=Decimal('100'), multiplier=Decimal('1'),
    )
    split_new_asset = live_policy_weight(
        policy=split_network, asset='ETH', master_position=Decimal('10'),
        master_mark=Decimal('2'), master_equity=Decimal('100'), multiplier=Decimal('1'),
    )
    known_unavailable = live_policy_weight(
        policy=split_network, asset='UNAV', master_position=Decimal('10'),
        master_mark=Decimal('2'), master_equity=Decimal('100'), multiplier=Decimal('1'),
    )

    assert same_new_asset == Decimal('0.2')
    assert split_new_asset == 0
    assert known_unavailable == 0


def test_exact_policy_never_compresses_current_master_exposure():
    weight = live_policy_weight(
        policy={'candidate_id': 'exact', 'selected_assets': [], 'allocation_scale': '0'},
        asset='SOL', master_position=Decimal('-5'), master_mark=Decimal('20'),
        master_equity=Decimal('1000'), multiplier=Decimal('1.5'),
    )
    assert weight == Decimal('-0.15')


def test_deterministic_fallback_returns_a_known_candidate():
    candidates = build_capital_candidates(
        master_positions={'A': Decimal('80'), 'B': Decimal('20')},
        master_mids={'A': '1', 'B': '1'},
        master_equity=Decimal('100'), follower_equity=Decimal('20'),
    )
    chosen = choose_deterministic_candidate(candidates, Decimal('95'))
    assert chosen['id'] in {'exact', 'smart_fidelity', 'smart_balanced', 'smart_defensive'}
