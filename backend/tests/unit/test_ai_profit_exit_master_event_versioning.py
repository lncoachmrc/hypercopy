import inspect

from app.services import copy as copy_service


def test_master_event_and_event_jobs_share_one_causal_order_allocation():
    source = inspect.getsource(copy_service.persist_master_fill_and_jobs)

    assert source.count("await next_master_leverage_causal_order(") == 1
    assert "causal_order=master_intent_order" in source
    assert "context['master_intent_order'] = master_intent_order" in source


def test_causal_order_is_allocated_before_master_event_persistence():
    source = inspect.getsource(copy_service.persist_master_fill_and_jobs)

    allocation = source.index("await next_master_leverage_causal_order(")
    event_creation = source.index("event = MasterEvent(")
    flush = source.index("await db.flush()")

    assert allocation < event_creation < flush


def test_replay_and_historical_events_are_versioned_before_early_return():
    source = inspect.getsource(copy_service.persist_master_fill_and_jobs)

    allocation = source.index("await next_master_leverage_causal_order(")
    historical_return = source.index(
        "if not create_copy_jobs or historical_event:"
    )

    assert allocation < historical_return
