import pytest
from app.engine.states import assert_execution_transition, assert_job_transition
from app.models.entities import ExecutionState, JobState


def test_valid_execution_unknown_resolution():
    assert_execution_transition(ExecutionState.SUBMITTING, ExecutionState.UNKNOWN)
    assert_execution_transition(ExecutionState.UNKNOWN, ExecutionState.FILLED)


def test_terminal_execution_cannot_reopen():
    with pytest.raises(ValueError):
        assert_execution_transition(ExecutionState.FILLED, ExecutionState.SUBMITTING)


def test_dead_job_only_manual_requeue():
    assert_job_transition(JobState.DEAD, JobState.QUEUED)
