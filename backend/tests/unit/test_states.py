import pytest

from app.engine.states import assert_execution_transition, assert_job_transition
from app.models.entities import CopyJob, Execution, ExecutionState, JobState


def test_valid_execution_unknown_resolution():
    assert_execution_transition(ExecutionState.SUBMITTING, ExecutionState.UNKNOWN)
    assert_execution_transition(ExecutionState.UNKNOWN, ExecutionState.FILLED)


def test_terminal_execution_cannot_reopen():
    with pytest.raises(ValueError):
        assert_execution_transition(ExecutionState.FILLED, ExecutionState.SUBMITTING)


def test_dead_job_only_manual_requeue():
    assert_job_transition(JobState.DEAD, JobState.QUEUED)


def test_job_runtime_assignment_enforces_transition_table():
    job = CopyJob(state=JobState.QUEUED)

    job.state = JobState.PROCESSING
    assert job.state is JobState.PROCESSING

    with pytest.raises(ValueError, match='Invalid job transition PROCESSING -> QUEUED'):
        job.state = JobState.QUEUED

    assert job.state is JobState.PROCESSING


def test_execution_runtime_assignment_enforces_transition_table():
    execution = Execution(state=ExecutionState.SUBMITTING)

    execution.state = ExecutionState.UNKNOWN
    execution.state = ExecutionState.FILLED
    assert execution.state is ExecutionState.FILLED

    with pytest.raises(ValueError, match='Invalid execution transition FILLED -> UNKNOWN'):
        execution.state = ExecutionState.UNKNOWN

    assert execution.state is ExecutionState.FILLED


def test_same_state_assignment_is_a_noop_not_a_transition():
    job = CopyJob(state=JobState.QUEUED)
    execution = Execution(state=ExecutionState.SUBMITTING)

    job.state = JobState.QUEUED
    execution.state = ExecutionState.SUBMITTING

    assert job.state is JobState.QUEUED
    assert execution.state is ExecutionState.SUBMITTING
