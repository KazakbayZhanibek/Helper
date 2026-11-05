"""Diagnostics measure attempts without changing evidence, call caps or results."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from ai_budget.models import AIUsageReservation
from media_pipeline.exceptions import Cancelled, ProcessingFailure
from media_pipeline.models import ProcessingJob
from media_pipeline.telemetry import AttemptMetrics, with_metrics
from media_pipeline.worker import JobContext, _finish, claim_job, process_one
from visual_drafts import worker
from .test_visual_full_video import full_video, install_model, queue


class Clock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_monotonic_stages_include_failures_and_snapshots_do_not_share_state():
    clock = Clock()
    metrics = AttemptMetrics(attempt=1, queue_wait_ms=12, clock=clock)
    with metrics.measure('frame_extraction'):
        clock.advance(0.25)
    with pytest.raises(Cancelled), metrics.measure('model_request'):
        clock.advance(2)
        raise Cancelled()
    snapshot = metrics.snapshot()
    assert snapshot['total_ms'] == 2250
    assert snapshot['stages']['frame_extraction'] == {'total_ms': 250, 'invocations': 1, 'errors': 0}
    assert snapshot['stages']['model_request'] == {'total_ms': 2000, 'invocations': 1, 'errors': 1}
    snapshot['stages']['model_request']['errors'] = 999
    assert metrics.snapshot()['stages']['model_request']['errors'] == 1
    metrics.finish('cancelled')
    clock.advance(20)
    assert metrics.snapshot()['total_ms'] == 2250
    assert metrics.snapshot()['outcome'] == 'cancelled'


def test_allowlist_rejects_source_text_and_arbitrary_labels():
    metrics = AttemptMetrics(attempt=1)
    with pytest.raises(ValueError), metrics.measure('/private/source.mp4'):
        pass
    with pytest.raises(ValueError):
        metrics.count('private model response')
    for value in (-1, True, 'secret'):
        with pytest.raises(ValueError):
            metrics.count('prepared_frames', value)
    assert '/private/' not in json.dumps(metrics.snapshot())


def test_retry_timings_are_per_attempt_and_do_not_mislabel_elapsed_time_as_queue():
    now = timezone.now()
    job = SimpleNamespace(attempts=1, started_at=now, created_at=now - timedelta(seconds=3))
    first = AttemptMetrics.for_job(job)
    assert first.snapshot()['queue_wait_ms'] == 3000
    first.count('completed_windows')
    job.attempts = 2
    retry = AttemptMetrics.for_job(job).snapshot()
    assert retry['queue_wait_ms'] is None
    assert retry['scope'] == 'current_attempt' and retry['attempt'] == 2
    assert retry['counters'] == {} and retry['stages'] == {}


def test_merging_diagnostics_preserves_budget_and_proposal_without_mutating_result():
    result = {'visual_proposal_id': 'proposal', 'analysis': {'budget': {'calls_used': 3}}}
    context = SimpleNamespace(metrics=AttemptMetrics(attempt=1))
    merged = with_metrics(result, context, outcome='done')
    assert 'metrics' not in result
    assert merged['visual_proposal_id'] == result['visual_proposal_id']
    assert merged['analysis'] == result['analysis']
    assert merged['metrics']['outcome'] == 'done'


@pytest.mark.django_db
def test_video_job_records_three_requests_and_keeps_exact_call_budget(team, full_video, monkeypatch):
    calls = install_model(monkeypatch)
    clock = Clock()
    metrics = AttemptMetrics(attempt=1, queue_wait_ms=0, clock=clock)
    monkeypatch.setattr(AttemptMetrics, 'for_job', classmethod(lambda cls, job: metrics))
    generate = worker.VisualProvider.generate

    def timed_generate(self, **kwargs):
        clock.advance(2)
        return generate(self, **kwargs)

    monkeypatch.setattr(worker.VisualProvider, 'generate', timed_generate)
    job, session = queue(team, full_video, max_ai_calls=3)
    assert process_one()
    job.refresh_from_db()
    result = job.result
    assert job.state == 'done' and calls == [0, 30000, 60000]
    assert result['visual_proposal_id'] and result['analysis']['budget']['calls_used'] == 3
    assert AIUsageReservation.objects.count() == 3
    metrics = result['metrics']
    assert metrics['outcome'] == 'done' and metrics['total_ms'] == 6000
    assert metrics['stages']['model_request'] == {'total_ms': 6000, 'invocations': 3, 'errors': 0}
    assert metrics['stages']['validation']['invocations'] == 3
    assert metrics['stages']['aggregation']['invocations'] == 1
    assert metrics['counters'] == {'prepared_frames': 6, 'completed_windows': 3}


@pytest.mark.django_db
def test_cancel_after_checkpoint_keeps_progress_and_final_attempt_metrics(team, full_video, monkeypatch):
    install_model(monkeypatch)
    progress = worker.publish_progress
    snapshots = []

    def interrupt(job, session, context):
        progress(job, session, context)
        snapshots.append(ProcessingJob.objects.get(pk=job.id).result)
        raise Cancelled()

    monkeypatch.setattr(worker, 'publish_progress', interrupt)
    job, session = queue(team, full_video, max_ai_calls=3)
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'cancelled'
    assert snapshots[0]['metrics']['outcome'] == 'running'
    assert snapshots[0]['analysis']['windows_completed'] == 1
    assert job.result['analysis'] == snapshots[0]['analysis']
    assert job.result['metrics']['outcome'] == 'cancelled'
    assert job.result['metrics']['counters']['completed_windows'] == 1
    assert AIUsageReservation.objects.count() == 1


@pytest.mark.django_db
def test_failure_before_first_checkpoint_keeps_timing_without_private_exception_text(team, full_video, monkeypatch):
    install_model(monkeypatch)

    def unreadable(frames):
        raise ProcessingFailure('VISUAL_FRAME_CHANGED', 'Проверка целостности не пройдена.')

    monkeypatch.setattr(worker, 'readable_frames', unreadable)
    job, session = queue(team, full_video, max_ai_calls=3)
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'failed' and job.error_code == 'VISUAL_FRAME_CHANGED'
    metrics = job.result['metrics']
    assert metrics['outcome'] == 'failed'
    assert metrics['stages']['frame_reading']['errors'] == 1
    assert 'Проверка' not in json.dumps(metrics, ensure_ascii=False)
    assert not AIUsageReservation.objects.exists()


@pytest.mark.django_db
def test_stale_attempt_cannot_overwrite_results_or_metrics(team, full_video):
    job, session = queue(team, full_video, max_ai_calls=3)
    claimed = claim_job()
    context = JobContext(claimed)
    checkpoint = {'analysis': {'windows_completed': 1}, 'metrics': {'attempt': 2}}
    ProcessingJob.objects.filter(pk=job.id).update(state='queued', lease_token=None, result=checkpoint)
    _finish(claimed, 'failed', context=context)
    job.refresh_from_db()
    assert job.result == checkpoint and job.state == 'queued'
