import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import Instruction, Membership
from media_pipeline.exceptions import Cancelled, ProcessingFailure
from media_pipeline.jobs import job_data, retry_job
from media_pipeline.media import validate_probe
from media_pipeline.models import ProcessingJob
from media_pipeline.worker import JobContext, claim_job, process_one, recover_expired_jobs
from .test_uploads import start, upload_url

pytestmark = pytest.mark.django_db


def installed_media_binary(name):
    configured = os.getenv(name.upper() + '_PATH', '')
    candidates = [configured, shutil.which(name), str(Path.home() / '.agents/skills/video-shotcraft/.runtime/bin' / name)]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    pytest.skip(f'{name} is not installed; set {name.upper()}_PATH to run real media tests')


def probe_data(**video_changes):
    return {'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 1920, 'height': 1080, 'duration': '12', **video_changes},
                        {'codec_type': 'audio', 'codec_name': 'aac'}], 'format': {'duration': '12'}}


@pytest.mark.parametrize('change,code', [({'codec_name': 'vp9'}, 'UNSUPPORTED_VIDEO_CODEC'),
    ({'width': 5000}, 'INVALID_RESOLUTION'), ({'duration': 'NaN'}, 'INVALID_DURATION')])
def test_rejects_unsupported_or_corrupt_metadata(change, code):
    with pytest.raises(ProcessingFailure) as error:
        validate_probe(probe_data(**change))
    assert error.value.code == code


def test_rotation_silent_video_and_nonfinite_timing():
    data = probe_data(side_data_list=[{'rotation': -90}])
    data['streams'] = data['streams'][:1]
    result = validate_probe(data)
    assert result == {'width': 1080, 'height': 1920, 'duration_ms': 12000, 'has_audio': False}
    data['format']['duration'] = 'Infinity'
    with pytest.raises(ProcessingFailure):
        validate_probe(data)


def queued_job(team, body=b'notvideo'):
    session = start(team, size=len(body)).json()
    response = team.clients['author'].post(upload_url(team, session['id']) + '/content', body, content_type='application/octet-stream')
    assert response.status_code == 202, response.data
    return ProcessingJob.objects.get(pk=response.json()['job_id'])


def test_durable_claim_fences_cancel_and_expired_attempt(team):
    job = queued_job(team)
    claimed = claim_job()
    assert claimed.id == job.id and claimed.attempts == 1
    assert claim_job() is None
    context = JobContext(claimed)
    context.check()
    ProcessingJob.objects.filter(pk=job.id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    recover_expired_jobs()
    next_attempt = claim_job()
    assert next_attempt.attempts == 2 and next_attempt.lease_token != claimed.lease_token
    with pytest.raises(Cancelled), transaction.atomic():
        context.commit_guard()
    ProcessingJob.objects.filter(pk=job.id).update(cancel_requested=True)
    with pytest.raises(Cancelled):
        JobContext(next_attempt).check()


def test_removed_membership_cancels_job_without_leaking_private_error(team):
    job = queued_job(team)
    Membership.objects.filter(organization=team.org, user=team.users['author']).update(is_active=False)
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'cancelled'


@pytest.mark.parametrize('state', ['failed', 'cancelled'])
def test_finish_preserves_durable_progress_on_interruption(team, state):
    from media_pipeline.worker import _finish
    queued_job(team)
    claimed = claim_job()
    checkpoint = {'visual_session_id': str(uuid.uuid4()), 'analysis': {'windows_total': 3, 'windows_completed': 1}}
    ProcessingJob.objects.filter(pk=claimed.pk).update(result=checkpoint)
    _finish(claimed, state)
    claimed.refresh_from_db()
    assert claimed.state == state
    assert claimed.result == checkpoint


def test_disabled_user_never_starts_media_handler(team, monkeypatch):
    job = queued_job(team)
    team.users['author'].is_active = False
    team.users['author'].save(update_fields=['is_active'])
    called = []
    monkeypatch.setattr('media_pipeline.media.prepare_media', lambda *args: called.append(True))
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'cancelled' and called == []


def test_subprocess_timeout_and_bounded_output(team):
    queued_job(team)
    context = JobContext(claim_job())
    with pytest.raises(ProcessingFailure) as error:
        context.run([sys.executable, '-c', 'import time; time.sleep(5)'], timeout=0.1)
    assert error.value.code == 'PROCESS_TIMEOUT'
    success = context.run([sys.executable, '-c', 'print("ok")'], timeout=5)
    assert success.stdout.strip() == 'ok'


def test_cancelled_context_does_not_start_provider_callable(team):
    job = queued_job(team)
    context = JobContext(claim_job())
    ProcessingJob.objects.filter(pk=job.id).update(cancel_requested=True)
    called = []
    with pytest.raises(Cancelled):
        context.call(lambda: called.append(True), timeout=5)
    assert called == []


def test_worker_rejects_nonvideo_without_exposing_original(team, settings):
    settings.FFPROBE_PATH = installed_media_binary('ffprobe')
    job = queued_job(team)
    assert process_one()
    job.refresh_from_db()
    job.asset.refresh_from_db()
    assert job.state == 'failed' and job.asset.status == 'rejected'
    assert job.asset.storage_key == ''
    assert 'quarantine' not in json.dumps(job_data(job))


def test_job_db_guard_rejects_mismatched_tenant(team):
    job = queued_job(team)
    with pytest.raises(IntegrityError), transaction.atomic():
        ProcessingJob.objects.filter(pk=job.id).update(organization=team.other)


def test_retry_after_committed_media_preserves_playable_derivatives(team, settings):
    job = queued_job(team)
    asset = job.asset
    asset.status = 'ready'
    asset.storage_key = 'completed/playback.mp4'
    asset.thumbnail_key = 'completed/thumbnail.jpg'
    asset.audio_key = 'completed/audio.wav'
    asset.duration_ms, asset.width, asset.height = 10000, 320, 240
    asset.save()
    job.state, job.stage, job.retryable, job.attempts = 'cancelled', 'cancelled', True, 1
    job.save()
    member = Membership.objects.get(organization=team.org, user=team.users['author'])
    retry_job(member, job.id)
    asset.refresh_from_db()
    assert asset.status == 'ready'
    assert process_one()
    job.refresh_from_db()
    asset.refresh_from_db()
    assert job.state == 'done' and asset.status == 'ready'
    assert (asset.storage_key, asset.thumbnail_key, asset.audio_key) == ('completed/playback.mp4', 'completed/thumbnail.jpg', 'completed/audio.wav')


def test_real_video_prepare_roundtrip(team, settings, tmp_path):
    settings.FFMPEG_PATH, settings.FFPROBE_PATH = installed_media_binary('ffmpeg'), installed_media_binary('ffprobe')
    source = tmp_path / 'test-source.mp4'
    subprocess.run([settings.FFMPEG_PATH, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x240:r=12',
        '-f', 'lavfi', '-i', 'sine=frequency=600:sample_rate=16000', '-t', '10.2', '-c:v', 'libx264', '-threads', '2',
        '-c:a', 'aac', '-pix_fmt', 'yuv420p', str(source)], check=True, timeout=30)
    job = queued_job(team, source.read_bytes())
    assert process_one()
    job.refresh_from_db()
    job.asset.refresh_from_db()
    assert job.state == 'done', (job.error_code, job.error_summary)
    asset = job.asset
    assert asset.status == 'ready' and asset.has_audio and asset.width == 320 and asset.height == 240
    assert 10000 <= asset.duration_ms <= 11000
    assert asset.original_key != asset.storage_key
    for key in (asset.storage_key, asset.thumbnail_key, asset.audio_key):
        path = settings.PRIVATE_MEDIA_ROOT / key
        assert path.is_file() and path.stat().st_size > 0
        assert path.stat().st_mode & 0o777 == 0o600
    playback = team.clients['author'].get(f'/api/v1/orgs/{team.org.id}/assets/{asset.id}/playback', HTTP_RANGE='bytes=0-19')
    assert playback.status_code == 206 and len(b''.join(playback.streaming_content)) == 20
    assert team.clients['reader'].get(f'/api/v1/orgs/{team.org.id}/assets/{asset.id}/playback').status_code == 404
