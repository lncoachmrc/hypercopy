"""TEMPORARY P0 regression negative-control harness.

This file exists only on the isolated evidence branch and MUST NOT be merged.
It deliberately neutralizes the two P0 remediations during pytest so the
regression tests prove they detect the pre-fix failure modes.
"""

from contextlib import asynccontextmanager


@asynccontextmanager
async def _no_position_ledger_lock(_user_id):
    yield


def pytest_sessionstart(session):
    del session

    import app.db.position_ledger_lock as lock_module
    import app.services.execution as execution
    import app.services.execution_resolution as execution_resolution
    import app.services.reconcile as reconcile

    # HF-002 negative control: remove the per-user PostgreSQL serialization.
    lock_module.position_ledger_lock = _no_position_ledger_lock
    execution.position_ledger_lock = _no_position_ledger_lock
    reconcile.position_ledger_lock = _no_position_ledger_lock

    # HF-001 negative control: make terminal jobs ineligible for quarantine,
    # reproducing the indefinite UNKNOWN fence after retry exhaustion.
    execution_resolution._TERMINAL_JOB_STATES = set()
