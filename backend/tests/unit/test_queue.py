from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.services.queue import stale_enqueue_cutoff


def test_stale_enqueue_cutoff_uses_job_lease_floor():
    now = datetime(2026, 8, 16, 21, 0, tzinfo=UTC)
    cutoff = stale_enqueue_cutoff(now)
    expected_seconds = max(settings.JOB_LEASE_SECONDS, 30)
    assert cutoff == now - timedelta(seconds=expected_seconds)
