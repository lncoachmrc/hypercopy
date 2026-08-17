from datetime import UTC, datetime

from app.workers.ai_intelligence_worker import _parse_ts, _signal_ts


def test_parse_ts_normalizes_utc():
    assert _parse_ts('2026-08-17T21:27:57Z') == datetime(2026, 8, 17, 21, 27, 57, tzinfo=UTC)


def test_signal_ts_reads_master_event_timestamp():
    payload = '{"master_event_id":"abc","event_ts":"2026-08-17T21:27:57+00:00"}'
    assert _signal_ts(payload) == datetime(2026, 8, 17, 21, 27, 57, tzinfo=UTC)


def test_signal_ts_is_safe_for_bad_payload():
    assert _signal_ts('not-json') is None
