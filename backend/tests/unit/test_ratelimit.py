from app.adapters.ratelimit import Budget, Priority


def test_budget_allocation_stays_within_global_hyperliquid_limit():
    budget=Budget()
    budget.validate()
    allocated=(
        budget.orders + budget.reconcile + budget.diagnostic
        + budget.master_state + budget.metadata + budget.reserve
    )
    assert allocated==budget.total_per_minute==1200


def test_master_state_has_dedicated_headroom_for_bursty_sources():
    budget=Budget()
    assert budget.allowance(Priority.MASTER_STATE)==300
    assert budget.master_state > budget.reconcile
    assert budget.orders > budget.master_state
