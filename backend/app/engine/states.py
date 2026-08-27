from sqlalchemy import event
from sqlalchemy.orm.attributes import NO_VALUE

from app.models.entities import CopyJob, Execution, ExecutionState, JobState

JOB_TRANSITIONS = {
    JobState.QUEUED: {JobState.PROCESSING, JobState.SKIPPED, JobState.DEAD},
    JobState.RETRYING: {JobState.PROCESSING, JobState.DEAD, JobState.SKIPPED},
    JobState.PROCESSING: {JobState.DONE, JobState.RETRYING, JobState.DEAD, JobState.SKIPPED},
    JobState.DONE: set(),
    JobState.SKIPPED: set(),
    JobState.DEAD: {JobState.QUEUED},
}

EXECUTION_TRANSITIONS = {
    ExecutionState.SUBMITTING: {
        ExecutionState.UNKNOWN,
        ExecutionState.QUARANTINED,
        ExecutionState.FILLED,
        ExecutionState.REJECTED,
        ExecutionState.CANCELED,
    },
    ExecutionState.UNKNOWN: {
        ExecutionState.QUARANTINED,
        ExecutionState.FILLED,
        ExecutionState.REJECTED,
        ExecutionState.CANCELED,
    },
    ExecutionState.QUARANTINED: set(),
    ExecutionState.FILLED: set(),
    ExecutionState.REJECTED: set(),
    ExecutionState.CANCELED: set(),
}


def assert_job_transition(old: JobState, new: JobState) -> None:
    if new not in JOB_TRANSITIONS[old]:
        raise ValueError(f'Invalid job transition {old.value} -> {new.value}')


def assert_execution_transition(old: ExecutionState, new: ExecutionState) -> None:
    if new not in EXECUTION_TRANSITIONS[old]:
        raise ValueError(f'Invalid execution transition {old.value} -> {new.value}')


def _guard_job_state(_target: CopyJob, value: JobState, oldvalue: JobState, _initiator):
    """Reject invalid application-level CopyJob mutations before assignment.

    SQLAlchemy uses ``NO_VALUE`` for a new object's first assignment. ORM row
    population bypasses normal attribute-set events, so loading existing rows is
    unaffected; active history ensures an expired persistent value is loaded
    before this listener validates a transition.
    """
    if oldvalue is not NO_VALUE and oldvalue is not None and oldvalue != value:
        assert_job_transition(oldvalue, value)
    return value


def _guard_execution_state(
    _target: Execution,
    value: ExecutionState,
    oldvalue: ExecutionState,
    _initiator,
):
    """Reject invalid application-level Execution mutations before assignment."""
    if oldvalue is not NO_VALUE and oldvalue is not None and oldvalue != value:
        assert_execution_transition(oldvalue, value)
    return value


# HF-009: the transition tables used to be test-only documentation. Register
# them at the ORM attribute boundary so every normal runtime mutation of these
# durable state machines is checked, including API, worker, queue and resolver
# paths, without relying on each caller to remember a separate assertion.
event.listen(CopyJob.state, 'set', _guard_job_state, retval=True, active_history=True)
event.listen(Execution.state, 'set', _guard_execution_state, retval=True, active_history=True)
