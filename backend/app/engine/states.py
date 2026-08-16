from app.models.entities import ExecutionState, JobState

JOB_TRANSITIONS = {
    JobState.QUEUED: {JobState.PROCESSING, JobState.SKIPPED, JobState.DEAD},
    JobState.RETRYING: {JobState.PROCESSING, JobState.DEAD, JobState.SKIPPED},
    JobState.PROCESSING: {JobState.DONE, JobState.RETRYING, JobState.DEAD, JobState.SKIPPED},
    JobState.DONE: set(), JobState.SKIPPED: set(), JobState.DEAD: {JobState.QUEUED},
}
EXECUTION_TRANSITIONS = {
    ExecutionState.SUBMITTING: {ExecutionState.UNKNOWN, ExecutionState.FILLED, ExecutionState.REJECTED, ExecutionState.CANCELED},
    ExecutionState.UNKNOWN: {ExecutionState.FILLED, ExecutionState.REJECTED, ExecutionState.CANCELED},
    ExecutionState.FILLED: set(), ExecutionState.REJECTED: set(), ExecutionState.CANCELED: set(),
}


def assert_job_transition(old: JobState, new: JobState) -> None:
    if new not in JOB_TRANSITIONS[old]:
        raise ValueError(f'Invalid job transition {old.value} -> {new.value}')


def assert_execution_transition(old: ExecutionState, new: ExecutionState) -> None:
    if new not in EXECUTION_TRANSITIONS[old]:
        raise ValueError(f'Invalid execution transition {old.value} -> {new.value}')
