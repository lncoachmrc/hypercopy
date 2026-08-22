from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.core.config import settings
from app.services.queue import stale_enqueue_cutoff, strategy_job_expired, strategy_job_expiry_cutoff


def test_stale_enqueue_cutoff_uses_job_lease_floor():
    now = datetime(2026, 8, 16, 21, 0, tzinfo=UTC)
    cutoff = stale_enqueue_cutoff(now)
    expected_seconds = max(settings.JOB_LEASE_SECONDS, 30)
    assert cutoff == now - timedelta(seconds=expected_seconds)


def test_strategy_job_expiry_cutoff_uses_bounded_age():
    now = datetime(2026, 8, 22, 16, 45, tzinfo=UTC)
    assert strategy_job_expiry_cutoff(now) == now - timedelta(seconds=settings.STRATEGY_JOB_MAX_AGE_SECONDS)


def test_old_event_and_reconcile_jobs_expire():
    now = datetime(2026, 8, 22, 16, 45, tzinfo=UTC)
    old = now - timedelta(seconds=settings.STRATEGY_JOB_MAX_AGE_SECONDS + 1)
    assert strategy_job_expired(SimpleNamespace(origin='EVENT', created_at=old), now) is True
    assert strategy_job_expired(SimpleNamespace(origin='RECONCILE', created_at=old), now) is True


def test_close_all_and_admin_jobs_do_not_expire_by_strategy_ttl():
    now = datetime(2026, 8, 22, 16, 45, tzinfo=UTC)
    old = now - timedelta(days=2)
    assert strategy_job_expired(SimpleNamespace(origin='CLOSE_ALL', created_at=old), now) is False
    assert strategy_job_expired(SimpleNamespace(origin='ADMIN_RECONCILE', created_at=old), now) is False
