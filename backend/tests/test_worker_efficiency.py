"""Cancellation fencing, bounded maintenance and decoder lock boundaries."""
import importlib
import io
import os
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.core.management import call_command, CommandError
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ai_budget.models import AIUsageReservation, BudgetAccount
from ai_budget.services import execute_budgeted_call
from core.http import DomainError
from core.models import Instruction, Membership
from media_pipeline import maintenance, worker
from media_pipeline.exceptions import Cancelled
from media_pipeline.jobs import enqueue_job
from media_pipeline.models import ProcessingJob
from visual_drafts import services, worker as vision_worker
from visual_drafts.models import FrameEvidence, VisualSession
from .test_media_pipeline import queued_job
from .test_video_frames import video_asset
from .test_visual_drafts import configured_vision, create_session, jpeg, job_for

pytestmark = pytest.mark.django_db


def clocked_context(team, monkeypatch):
    queued_job(team)
    tick = [10.0]
    monkeypatch.setattr(worker.time, 'monotonic', lambda: tick[0])
    context = worker.JobContext(worker.claim_job())
    return context, tick


def test_poll_is_one_read_no_write_then_cached_for_half_second(team, monkeypatch):
    context, tick = clocked_context(team, monkeypatch)
    with CaptureQueriesContext(connection) as first:
        context.check()
    assert len(first) == 1 and first[0]['sql'].lstrip().startswith('SELECT')
    with CaptureQueriesContext(connection) as burst:
        for _ in range(100):
            context.check()
    assert len(burst) == 0
    tick[0] += worker.CHECK_SECONDS
    with CaptureQueriesContext(connection) as next_poll:
        context.check()
    assert len(next_poll) == 1 and next_poll[0]['sql'].lstrip().startswith('SELECT')
    tick[0] += worker.HEARTBEAT_SECONDS
    with CaptureQueriesContext(connection) as heartbeat:
        context.check()
    assert len(heartbeat) == 2
    assert sum(q['sql'].lstrip().startswith('UPDATE') for q in heartbeat) == 1


@pytest.mark.parametrize('change', ['cancel', 'lease', 'membership', 'user', 'organization', 'instruction', 'asset', 'delete'])
def test_dispatch_rechecks_every_revocation_even_during_cached_interval(team, monkeypatch, change):
    context, _ = clocked_context(team, monkeypatch)
    context.check()
    job = ProcessingJob.objects.get(pk=context.job_id)
    if change == 'cancel':
        ProcessingJob.objects.filter(pk=job.pk).update(cancel_requested=True)
    elif change == 'lease':
        ProcessingJob.objects.filter(pk=job.pk).update(lease_token=uuid.uuid4())
    elif change == 'membership':
        Membership.objects.filter(organization=team.org, user=team.users['author']).update(is_active=False)
    elif change == 'user':
        team.users['author'].is_active = False
        team.users['author'].save(update_fields=['is_active'])
    elif change == 'organization':
        team.org.status = 'suspended'
        team.org.save(update_fields=['status'])
    elif change == 'instruction':
        Instruction.objects.filter(pk=job.instruction_id).update(lifecycle='deleted')
    elif change == 'asset':
        job.asset.status = 'deleted'
        job.asset.save(update_fields=['status'])
    else:
        job.delete()
    with pytest.raises(Cancelled):
        context.call(lambda: pytest.fail('outbound callable started'), timeout=1)


def test_budget_guard_refunds_unstarted_call_after_cached_cancel(team, monkeypatch):
    context, _ = clocked_context(team, monkeypatch)
    context.check()
    job = ProcessingJob.objects.get(pk=context.job_id)
    ProcessingJob.objects.filter(pk=job.id).update(cancel_requested=True)
    provider = SimpleNamespace(provider='codex', model='gpt-6-luna')
    with pytest.raises(Cancelled):
        execute_budgeted_call(provider, job=job, context=context, call_key='cancelled-before-dispatch',
                              invoke=lambda: pytest.fail('model request sent'))
    assert AIUsageReservation.objects.get().state == 'released'
    assert all(a.spent == a.reserved == 0 for a in BudgetAccount.objects.all())


def test_commit_guard_is_never_throttled(team, monkeypatch):
    context, _ = clocked_context(team, monkeypatch)
    context.check()
    ProcessingJob.objects.filter(pk=context.job_id).update(cancel_requested=True)
    with pytest.raises(Cancelled), transaction.atomic():
        context.commit_guard()


def test_known_expiry_bypasses_cache(team, monkeypatch):
    context, _ = clocked_context(team, monkeypatch)
    context.check()
    future = context.checked_lease_expires_at + timedelta(seconds=1)
    monkeypatch.setattr(worker.timezone, 'now', lambda: future)
    with pytest.raises(Cancelled):
        context.check()


def test_loop_survives_maintenance_and_iteration_failure_without_private_logs(monkeypatch, caplog):
    command = importlib.import_module('media_pipeline.management.commands.processing_worker')
    calls, sleeps, cleanups = [], [], []
    def process():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('private-provider-error')
        if len(calls) == 3:
            raise KeyboardInterrupt()
        return True
    def cleanup():
        cleanups.append(1)
        raise RuntimeError('private-file-name')
    monkeypatch.setattr(command, 'process_one', process)
    monkeypatch.setattr(command, 'maintenance_tick', cleanup)
    monkeypatch.setattr(command, 'close_old_connections', lambda: None)
    monkeypatch.setattr(command.time, 'monotonic', lambda: 10)
    monkeypatch.setattr(command.time, 'sleep', sleeps.append)
    call_command('processing_worker', poll_seconds=0.01, stdout=io.StringIO())
    assert len(calls) == 3 and len(cleanups) == 1 and sleeps == [1.0]
    assert 'private-provider-error' not in caplog.text and 'private-file-name' not in caplog.text


def test_once_reports_failure_instead_of_success_exit(monkeypatch):
    command = importlib.import_module('media_pipeline.management.commands.processing_worker')
    monkeypatch.setattr(command, 'maintenance_tick', lambda: None)
    monkeypatch.setattr(command, 'close_old_connections', lambda: None)
    def fail():
        raise RuntimeError('private')
    monkeypatch.setattr(command, 'process_one', fail)
    with pytest.raises(CommandError):
        call_command('processing_worker', once=True, stdout=io.StringIO())


def derived_batch(root, *, token=None):
    path = root / 'derived' / str(uuid.uuid4()) / str(uuid.uuid4()) / str(token or uuid.uuid4())
    path.mkdir(parents=True)
    (path / 'playback.mp4').write_bytes(b'synthetic stale output')
    old = (timezone.now() - timedelta(days=2)).timestamp()
    os.utime(path, (old, old))
    return path


def test_derived_cleanup_batches_queries_instead_of_per_directory(settings):
    directories = [derived_batch(settings.PRIVATE_MEDIA_ROOT) for _ in range(40)]
    with CaptureQueriesContext(connection) as queries:
        assert maintenance.cleanup_derived_files(settings.PRIVATE_MEDIA_ROOT, now=timezone.now()) == 40
    assert len(queries) == 2
    assert not any(p.exists() for p in directories)


@pytest.mark.parametrize('field', ['storage_key', 'original_key', 'audio_key', 'thumbnail_key'])
def test_derived_cleanup_preserves_all_asset_references(team, settings, field):
    path = derived_batch(settings.PRIVATE_MEDIA_ROOT)
    job = queued_job(team)
    setattr(job.asset, field, (path / 'playback.mp4').relative_to(settings.PRIVATE_MEDIA_ROOT).as_posix())
    job.asset.save(update_fields=[field])
    assert maintenance.cleanup_derived_files(settings.PRIVATE_MEDIA_ROOT, now=timezone.now()) == 0
    assert path.exists()


def test_derived_cleanup_preserves_active_lease_and_bounds_batch(team, settings, monkeypatch):
    queued_job(team)
    job = worker.claim_job()
    active = derived_batch(settings.PRIVATE_MEDIA_ROOT, token=job.lease_token)
    assert maintenance.cleanup_derived_files(settings.PRIVATE_MEDIA_ROOT, now=timezone.now()) == 0
    stale = [derived_batch(settings.PRIVATE_MEDIA_ROOT) for _ in range(4)]
    monkeypatch.setattr(maintenance, 'MAX_DERIVED_CANDIDATES', 2)
    assert maintenance.cleanup_derived_files(settings.PRIVATE_MEDIA_ROOT, now=timezone.now()) <= 2
    assert active.exists() and sum(p.exists() for p in stale) >= 2


def test_session_column_populated_at_enqueue_and_backfilled_without_payload_mutation(team, draft):
    session = create_session(team, draft)
    member = Membership.objects.get(organization=team.org, user=team.users['author'])
    payload = {'visual_session_id': str(session.id), 'frame_ids': []}
    job = enqueue_job(member, session.instruction, 'analyze_live', payload)
    assert job.visual_session_id == session.id
    ProcessingJob.objects.filter(pk=job.id).update(visual_session_id=None)
    migration = importlib.import_module('media_pipeline.migrations.0004_processingjob_visual_session_id')
    migration.backfill_sessions(apps, SimpleNamespace(connection=connection))
    job.refresh_from_db()
    assert job.visual_session_id == session.id and job.payload == payload
    with connection.cursor() as cursor:
        indexes = connection.introspection.get_constraints(cursor, ProcessingJob._meta.db_table)
    assert any(row['index'] and row['columns'] == ['visual_session_id'] for row in indexes.values())
    with connection.cursor() as cursor:
        indexes = connection.introspection.get_constraints(cursor, VisualSession._meta.db_table)
    assert indexes['visual_session_state_expiry']['columns'] == ['state', 'expires_at']


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('cancel_during_decode', [False, True])
def test_camera_decodes_outside_transaction_and_rechecks_session(team, draft, monkeypatch, cancel_during_decode):
    session = create_session(team, draft)
    member = Membership.objects.get(organization=team.org, user=team.users['author'])
    decode = services.decoded_jpeg
    def checked_decode(raw):
        assert not connection.in_atomic_block
        if cancel_during_decode:
            VisualSession.objects.filter(pk=session.id).update(state='cancelled')
        return decode(raw)
    monkeypatch.setattr(services, 'decoded_jpeg', checked_decode)
    if cancel_during_decode:
        with pytest.raises(DomainError):
            services.receive_frame(member, session.id, io.BytesIO(jpeg()), sequence=1, timestamp_ms=0, key='frame')
        assert not FrameEvidence.objects.exists()
    else:
        _, status = services.receive_frame(member, session.id, io.BytesIO(jpeg()), sequence=1, timestamp_ms=0, key='frame')
        assert status == 201 and FrameEvidence.objects.count() == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('cancel_during_decode', [False, True])
def test_uploaded_batch_decodes_outside_lock_and_fences_cancel(video_asset, team, monkeypatch, cancel_during_decode):
    asset, _ = video_asset
    instruction = Instruction.objects.get(source_asset=asset)
    session = VisualSession.objects.create(organization=team.org, instruction=instruction, asset=asset,
        created_by=team.users['author'], mode='upload', consent_images=True,
        base_working_revision=instruction.working_copy.revision, language='ru',
        expires_at=timezone.now() + timedelta(minutes=10), window_start_ms=0, window_end_ms=2000)
    job = job_for(team, session, [])
    claimed = worker.claim_job()
    assert claimed.pk == job.pk
    context = worker.JobContext(claimed)
    decode, seen = vision_worker.decoded_jpeg, []
    def checked_decode(*args, **kwargs):
        assert not connection.in_atomic_block
        seen.append(1)
        if cancel_during_decode:
            ProcessingJob.objects.filter(pk=job.id).update(cancel_requested=True)
        return decode(*args, **kwargs)
    monkeypatch.setattr(vision_worker, 'decoded_jpeg', checked_decode)
    if cancel_during_decode:
        with pytest.raises(Cancelled):
            vision_worker.uploaded_frames(claimed, session, context)
        assert not FrameEvidence.objects.exists()
    else:
        assert len(vision_worker.uploaded_frames(claimed, session, context)) == 12
        assert len(seen) == 12
        assert context.metrics.snapshot()['stages']['frame_decode']['invocations'] == 1
