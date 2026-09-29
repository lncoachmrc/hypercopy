"""RED contracts: the observability fallback must not re-read an exhausted lane.

RateLimitExhausted is not an exchange 429, so today the fallback treats it as a
generic reconcile failure and immediately re-reads every follower snapshot on
the same RECONCILE lane that has just been exhausted.
"""

from __future__ import annotations

import pytest

from app.adapters.ratelimit import RateLimitExhausted
from app.workers import execution_worker
from app.workers.execution_worker import Worker


class RecordingLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def _record(self, level: str, message: str, *args, **kwargs) -> None:
        self.records.append((level, message, dict(kwargs.get("extra") or {})))

    def warning(self, message: str, *args, **kwargs) -> None:
        self._record("warning", message, *args, **kwargs)

    def info(self, message: str, *args, **kwargs) -> None:
        self._record("info", message, *args, **kwargs)

    def error(self, message: str, *args, **kwargs) -> None:
        self._record("error", message, *args, **kwargs)

    def exception(self, message: str, *args, **kwargs) -> None:
        self._record("exception", message, *args, **kwargs)

    def debug(self, message: str, *args, **kwargs) -> None:
        self._record("debug", message, *args, **kwargs)


def _worker(refresh_calls: list[str]) -> Worker:
    worker = Worker.__new__(Worker)

    async def refresh(_db, network):
        refresh_calls.append(network)
        return 3

    worker._refresh_follower_observability = refresh  # type: ignore[method-assign]
    return worker


@pytest.mark.asyncio
async def test_fallback_skips_reads_after_limiter_exhaustion(monkeypatch) -> None:
    recording_log = RecordingLog()
    monkeypatch.setattr(execution_worker, "log", recording_log)

    baseline_calls: list[str] = []
    baseline_result = await _worker(baseline_calls)._fallback_observability_after_reconcile_failure(
        object(),
        "testnet",
        RuntimeError("database connection reset"),
    )

    exhausted_calls: list[str] = []
    exhausted_result = await _worker(exhausted_calls)._fallback_observability_after_reconcile_failure(
        object(),
        "testnet",
        RateLimitExhausted("No headroom for weight 22 on the RECONCILE lane after 15s"),
    )

    deferred = [
        extra for _level, _message, extra in recording_log.records
        if extra.get("event_code") == "FOLLOWER_OBSERVABILITY_LIMITER_DEFERRED"
    ]

    assert baseline_calls == ["testnet"], (
        "baseline: a generic reconcile failure must still refresh observability"
    )
    assert baseline_result == 3

    assert exhausted_calls == [], (
        "RED C3: after RateLimitExhausted the fallback must not re-read follower "
        f"snapshots on the exhausted lane, refresh ran for {exhausted_calls}"
    )
    assert exhausted_result == 0
    assert len(deferred) == 1, (
        "RED C3: limiter exhaustion must log FOLLOWER_OBSERVABILITY_LIMITER_DEFERRED, "
        f"got {recording_log.records}"
    )
    assert deferred[0].get("follower_network") == "testnet"
