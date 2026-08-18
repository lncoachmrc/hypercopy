import uuid

from app.api.admin import _reconcile_job_payload
from app.models.entities import CopyJob, JobState


def test_reconcile_job_payload_exposes_worker_completion():
    job_id = uuid.uuid4()
    job = CopyJob(
        id=job_id,
        user_id=uuid.uuid4(),
        asset='__RECONCILE__',
        origin='ADMIN_RECONCILE',
        state=JobState.DONE,
        attempt_count=1,
        correlation_id=uuid.uuid4().hex,
        context={
            'result': {'status': 'OK', 'jobs_created': 2},
            'stream_published': 2,
            'completed_at': '2026-08-18T20:00:00+00:00',
        },
    )

    assert _reconcile_job_payload(job) == {
        'job_id': str(job_id),
        'state': 'DONE',
        'last_error': None,
        'attempt_count': 1,
        'next_attempt_at': None,
        'result': {'status': 'OK', 'jobs_created': 2},
        'stream_published': 2,
        'completed_at': '2026-08-18T20:00:00+00:00',
    }


def test_reconcile_job_payload_handles_incomplete_job():
    job = CopyJob(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        asset='__RECONCILE__',
        origin='ADMIN_RECONCILE',
        state=JobState.RETRYING,
        attempt_count=2,
        correlation_id=uuid.uuid4().hex,
        last_error='TimeoutError',
        context={'reason': 'Operational control'},
    )

    payload = _reconcile_job_payload(job)
    assert payload['state'] == 'RETRYING'
    assert payload['attempt_count'] == 2
    assert payload['last_error'] == 'TimeoutError'
    assert payload['result'] is None
    assert payload['stream_published'] == 0
