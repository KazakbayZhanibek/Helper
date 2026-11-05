"""Synthetic codec fixtures test extraction/transport only, never action recognition."""
import hashlib
import json
import os
import subprocess
import uuid
from fractions import Fraction
from pathlib import Path

import pytest
from django.db import IntegrityError, transaction

from core.models import Instruction, Membership
from media_pipeline import frames
from media_pipeline.exceptions import Cancelled, ProcessingFailure
from media_pipeline.jobs import enqueue_job, job_data
from media_pipeline.models import ProcessingJob
from media_pipeline.worker import JobContext, claim_job, process_one
from .test_uploads import new_instruction, start, upload_url
from .test_media_pipeline import installed_media_binary, queued_job

pytestmark = pytest.mark.django_db


@pytest.fixture
def video_asset(team, settings, tmp_path):
    settings.FFMPEG_PATH = installed_media_binary('ffmpeg')
    settings.FFPROBE_PATH = installed_media_binary('ffprobe')
    path = tmp_path / 'transport-only.mp4'
    subprocess.run([settings.FFMPEG_PATH, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=12',
        '-t', '2', '-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p', str(path)], check=True, timeout=30)
    job = queued_job(team, path.read_bytes())
    asset = job.asset
    asset.storage_key, asset.duration_ms, asset.status = path.name, 2000, 'ready'
    asset.width, asset.height = 1280, 720
    asset.save()
    return asset, JobContext(claim_job())


def test_real_frames_have_actual_pts_private_paths_hashes_and_cleanup(video_asset, settings):
    asset, context = video_asset
    with frames.extract_video_frames(asset, context, start_ms=50, end_ms=1900, count=4) as batch:
        assert len(batch.frames) == 4
        assert all(50000 <= frame.timestamp_us < 1900000 for frame in batch.frames)
        assert batch.frames[0].requested_timestamp_ms == 50
        assert batch.frames[0].timestamp_us == 83333  # Actual 12-fps presentation time, not the 50-ms target.
        assert batch.frames[-1].timestamp_us == 1833333
        for frame in batch.frames:
            assert max(frame.width, frame.height) <= 960
            assert frame.path.is_relative_to(settings.PRIVATE_MEDIA_ROOT)
            assert frame.path.stat().st_mode & 0o777 == 0o600
            assert hashlib.sha256(frame.path.read_bytes()).hexdigest() == frame.sha256
        manifest = batch.manifest()
        assert manifest['sparse_sampling'] and manifest['largest_unsampled_interval_us'] > 0
        assert 'path' not in json.dumps(manifest) and 'temporary-frames' not in json.dumps(manifest)
        first_times = [frame.timestamp_us for frame in batch.frames]
        directory = batch.directory
    assert batch.closed and not directory.exists()
    with frames.extract_video_frames(asset, context, start_ms=50, end_ms=1900, count=4) as again:
        assert [frame.timestamp_us for frame in again.frames] == first_times
        assert again.id != batch.id


def test_cancellation_removes_only_own_temporary_batch(video_asset, settings, monkeypatch):
    asset, context = video_asset
    original_run = context.run
    def cancel_after_decode(argv, timeout):
        result = original_run(argv, timeout)
        if argv[0] == settings.FFMPEG_PATH:
            assert list(settings.PRIVATE_MEDIA_ROOT.glob('temporary-frames/**/*.jpg'))
            raise Cancelled()
        return result
    monkeypatch.setattr(context, 'run', cancel_after_decode)
    with pytest.raises(Cancelled):
        frames.extract_video_frames(asset, context, count=3)
    assert not list(settings.PRIVATE_MEDIA_ROOT.glob('temporary-frames/**/*.jpg'))
    assert (settings.PRIVATE_MEDIA_ROOT / asset.storage_key).is_file()


def test_batch_decoder_preserves_legacy_pts_and_hashes_with_one_process(video_asset):
    asset, context = video_asset
    with frames.extract_video_frames(asset, context, count=12, strategy='uniform_legacy') as baseline:
        expected = [(frame.pts, frame.time_base, frame.sha256) for frame in baseline.frames]
        assert baseline.manifest()['processing']['ffmpeg_processes'] == 12
    with frames.extract_video_frames(asset, context, count=12, strategy='uniform') as batch:
        assert [(frame.pts, frame.time_base, frame.sha256) for frame in batch.frames] == expected
        processing = batch.manifest()['processing']
        assert processing['ffmpeg_processes'] == 1 and processing['ffprobe_processes'] == 1
        assert processing['decode_ms'] > 0 and processing['total_ms'] >= processing['decode_ms']


def test_short_event_strategy_reuses_scan_and_keeps_verified_private_source_pts(video_asset, monkeypatch, settings):
    from media_pipeline import event_sampling
    asset, context = video_asset
    scan_calls = []

    def known_signal(*args, **kwargs):
        scan_calls.append(kwargs['duration_ms'])
        return event_sampling.EventScan(tuple(event_sampling.EventPoint(stamp, .1 if stamp in (600, 1400) else 0)
                                             for stamp in range(0, 2000, 200)), {})

    monkeypatch.setattr(event_sampling, 'scan_events', known_signal)
    with frames.extract_video_frames(asset, context, count=12, strategy='events') as original:
        assert original.manifest()['processing']['event_cache_hit'] is False
        assert original.selection['policy'] == event_sampling.EVENT_SAMPLING_VERSION
    with frames.extract_video_frames(asset, context, count=12, strategy='short_events') as batch:
        manifest = batch.manifest()
        assert len(batch.frames) == 12
        assert manifest['processing']['event_cache_hit'] is True
        assert manifest['processing']['index_cache_hit'] is True
        assert manifest['processing']['ffprobe_processes'] == 0
        assert manifest['processing']['ffmpeg_processes'] == 1
        assert batch.sampling_version == 'short-pairs-decoded-pts-v2'
        assert len(batch.selection['event_groups']) == 2
        times = [frame.timestamp_ms for frame in batch.frames]
        assert times == sorted(set(times))
        assert times[0] == 0 and times[-1] == 1916
        for group in batch.selection['event_groups']:
            assert set(group['selected_timestamps_ms']) <= set(times)
        for frame in batch.frames:
            assert frame.path.stat().st_mode & 0o777 == 0o600
            assert max(frame.width, frame.height) <= 960
            assert hashlib.sha256(frame.path.read_bytes()).hexdigest() == frame.sha256
        directory = batch.directory
    assert scan_calls == [2000]
    assert not directory.exists()
    assert not list(settings.PRIVATE_MEDIA_ROOT.glob('temporary-frames/**/*.jpg'))


def test_short_small_allowance_skips_scan_that_cannot_add_a_pair(video_asset, monkeypatch):
    from media_pipeline import event_sampling
    asset, context = video_asset
    def unexpected_scan(*args, **kwargs):
        raise AssertionError('Eight-image short policy should not run a change scan')
    monkeypatch.setattr(event_sampling, 'scan_events', unexpected_scan)
    with frames.extract_video_frames(asset, context, count=8, strategy='short_events') as batch:
        assert len(batch.frames) == 8
        assert batch.manifest()['processing']['ffmpeg_processes'] == 1
        assert batch.selection['event_targets_ms'] == []


def test_window_index_cache_reuses_hash_and_probe_only_within_attempt(video_asset, monkeypatch):
    asset, context = video_asset
    calls = []
    original_run = context.run
    def counted(argv, timeout):
        calls.append(Path(argv[0]).name)
        return original_run(argv, timeout)
    monkeypatch.setattr(context, 'run', counted)
    with frames.extract_video_frames(asset, context, start_ms=0, end_ms=1000, count=4) as first:
        digest = first.playback_sha256
        assert not first.processing['index_cache_hit']
    with frames.extract_video_frames(asset, context, start_ms=1000, end_ms=2000, count=4) as second:
        assert second.playback_sha256 == digest and second.processing['index_cache_hit']
        assert second.processing['hash_ms'] == second.processing['index_ms'] == 0
    assert calls.count('ffprobe') == 1 and calls.count('ffmpeg') == 2
    other_context = JobContext(ProcessingJob.objects.get(pk=context.job_id))
    with frames.extract_video_frames(asset, other_context, count=4) as fresh:
        assert not fresh.processing['index_cache_hit']


def test_event_scan_runs_once_per_source_and_keeps_same_image_allowance(video_asset):
    asset, context = video_asset
    with frames.extract_video_frames(asset, context, count=12, strategy='events') as first:
        assert 2 <= len(first.frames) <= 12
        assert first.processing['ffmpeg_processes'] == 2
        assert first.processing['ffprobe_processes'] == 1
        assert not first.processing['event_cache_hit']
        assert first.selection['semantic_actions_detected'] is False
    with frames.extract_video_frames(asset, context, count=12, strategy='events') as second:
        assert second.processing['event_cache_hit'] and second.processing['index_cache_hit']
        assert second.processing['ffmpeg_processes'] == 1 and second.processing['ffprobe_processes'] == 0
        assert second.processing['event_scan_ms'] == 0


def test_configured_uniform_rollback_avoids_event_scan(video_asset, settings):
    asset, context = video_asset
    settings.VIDEO_FRAME_STRATEGY = 'uniform'
    with frames.extract_video_frames(asset, context, count=12) as batch:
        assert batch.processing['strategy'] == 'uniform' and batch.processing['ffmpeg_processes'] == 1
        assert batch.sampling_version == 'decoded-pts-nearest-time-v1'
        assert context._video_source_index['events'] is None


@pytest.mark.parametrize('strategy', ['uniform', 'uniform_legacy'])
def test_cached_source_rejects_in_place_change_with_restored_size_and_mtime(video_asset, settings, strategy):
    asset, context = video_asset
    with frames.extract_video_frames(asset, context, count=4, strategy=strategy):
        pass
    path = settings.PRIVATE_MEDIA_ROOT / asset.storage_key
    before = path.stat()
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ProcessingFailure) as error:
        frames.extract_video_frames(asset, context, count=4, strategy=strategy)
    assert error.value.code == 'FRAME_SOURCE_CHANGED'
    assert not list(settings.PRIVATE_MEDIA_ROOT.glob('temporary-frames/**/*.jpg'))


def test_source_changed_during_batched_decode_cannot_become_evidence(video_asset, settings, monkeypatch):
    asset, context = video_asset
    original_run = context.run
    def change_after_decode(argv, timeout):
        result = original_run(argv, timeout)
        if argv[0] == settings.FFMPEG_PATH:
            path = settings.PRIVATE_MEDIA_ROOT / asset.storage_key
            data = bytearray(path.read_bytes())
            data[-1] ^= 1
            path.write_bytes(data)
        return result
    monkeypatch.setattr(context, 'run', change_after_decode)
    with pytest.raises(ProcessingFailure) as error:
        frames.extract_video_frames(asset, context, count=4)
    assert error.value.code == 'FRAME_SOURCE_CHANGED'
    assert not list(settings.PRIVATE_MEDIA_ROOT.glob('temporary-frames/**/*.jpg'))


def test_batched_vfr_b_frames_preserve_exact_timestamps(team, settings, tmp_path):
    settings.FFMPEG_PATH = installed_media_binary('ffmpeg')
    settings.FFPROBE_PATH = installed_media_binary('ffprobe')
    path = tmp_path / 'vfr-with-b-frames.mp4'
    subprocess.run([settings.FFMPEG_PATH, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=20',
        '-t', '2', '-vf', 'select=not(eq(mod(n\\,5)\\,1))', '-fps_mode', 'vfr', '-c:v', 'libx264', '-bf', '3',
        '-threads', '2', '-pix_fmt', 'yuv420p', str(path)], check=True, timeout=30)
    asset = queued_job(team, path.read_bytes()).asset
    asset.storage_key, asset.duration_ms, asset.status = path.name, 2000, 'ready'
    asset.width, asset.height = 320, 240
    asset.save()
    context = JobContext(claim_job())
    with frames.extract_video_frames(asset, context, count=12, strategy='uniform_legacy') as baseline:
        expected = [(frame.timestamp_us, frame.pts, frame.time_base, frame.sha256) for frame in baseline.frames]
    with frames.extract_video_frames(asset, context, count=12, strategy='uniform') as batch:
        assert [(frame.timestamp_us, frame.pts, frame.time_base, frame.sha256) for frame in batch.frames] == expected
        index = context._video_source_index['points']
        assert len({b - a for a, b in zip(index, index[1:])}) > 1


@pytest.mark.parametrize('kwargs', [{'count': 13}, {'start_ms': -1}, {'end_ms': 2001}, {'count': True}, {'start_ms': 1000, 'end_ms': 1000}])
def test_invalid_interval_and_count_never_decode(video_asset, monkeypatch, kwargs):
    asset, context = video_asset
    called = []
    monkeypatch.setattr(context, 'run', lambda *args, **kwargs: called.append(True))
    with pytest.raises(ProcessingFailure) as error:
        frames.extract_video_frames(asset, context, **kwargs)
    assert error.value.code == 'FRAME_INTERVAL_INVALID' and called == []


def test_frame_size_limit_cleans_partial_files(video_asset, settings, monkeypatch):
    asset, context = video_asset
    monkeypatch.setattr(frames, 'MAX_FRAME_BYTES', 100)
    with pytest.raises(ProcessingFailure):
        frames.extract_video_frames(asset, context, count=1)
    assert not list(settings.PRIVATE_MEDIA_ROOT.glob('temporary-frames/**/*.jpg'))


def test_lease_asset_binding_prevents_extracting_another_video(team, video_asset):
    asset, context = video_asset
    other = queued_job(team).asset
    with pytest.raises(ProcessingFailure) as error:
        frames.extract_video_frames(other, context, count=1)
    assert error.value.code == 'FRAME_SOURCE_MISMATCH'


def test_vfr_sampling_uses_actual_points_and_exposes_gaps():
    selected = frames._select_samples([0, 80, 120, 400, 401, 950], Fraction(1, 1000), 0, 1000, 4)
    assert selected[0] == (0, 0) and selected[-1] == (999, 950)
    assert all(pts in (0, 80, 120, 400, 401, 950) for _, pts in selected)
    with pytest.raises(ProcessingFailure):
        frames._decoded_metadata('[Parsed_showinfo_0] config in time_base: 1/1000\n[Parsed_showinfo_0] n: 0 pts: 401 pts_time:0.401 s:960x540', 400, Fraction(1, 1000))


def test_live_jobs_have_no_asset_and_keep_original_kind_guards(team):
    instruction = Instruction.objects.get(pk=new_instruction(team))
    member = Membership.objects.get(organization=team.org, user=team.users['author'])
    job = enqueue_job(member, instruction, 'analyze_live', {'visual_session_id': str(uuid.uuid4())})
    assert job.asset_id is None and job_data(job)['asset_id'] is None and not job_data(job)['manual_allowed']
    context = JobContext(claim_job())
    context.check()
    with pytest.raises(IntegrityError), transaction.atomic():
        ProcessingJob.objects.create(organization=team.org, instruction=instruction, requested_by=team.users['author'], kind='prepare_media')
    with pytest.raises(IntegrityError), transaction.atomic():
        ProcessingJob.objects.filter(pk=job.id).update(organization=team.other)


def test_streamed_webm_without_duration_normalizes_to_h264(team, settings, tmp_path):
    settings.FFMPEG_PATH = installed_media_binary('ffmpeg')
    settings.FFPROBE_PATH = installed_media_binary('ffprobe')
    source = tmp_path / 'camera-transport.webm'
    subprocess.run([settings.FFMPEG_PATH, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=12',
        '-f', 'lavfi', '-i', 'sine=frequency=600:sample_rate=48000', '-t', '10.2', '-c:v', 'libvpx-vp9', '-threads', '2',
        '-deadline', 'realtime', '-cpu-used', '8', '-c:a', 'libopus', '-live', '1', str(source)], check=True, timeout=30)
    inspected = subprocess.run([settings.FFPROBE_PATH, '-v', 'error', '-show_format', '-of', 'json', str(source)],
        check=True, capture_output=True, text=True, timeout=10)
    assert 'duration' not in json.loads(inspected.stdout)['format']
    session = start(team, size=source.stat().st_size, filename='camera.webm').json()
    response = team.clients['author'].post(upload_url(team, session['id']) + '/content', source.read_bytes(), content_type='application/octet-stream')
    assert response.status_code == 202
    job = ProcessingJob.objects.get(pk=response.json()['job_id'])
    assert process_one()
    job.refresh_from_db()
    job.asset.refresh_from_db()
    assert job.state == 'done', (job.error_code, job.error_summary)
    assert job.asset.status == 'ready' and job.asset.has_audio and 10000 <= job.asset.duration_ms <= 11000
    playback = settings.PRIVATE_MEDIA_ROOT / job.asset.storage_key
    result = subprocess.run([settings.FFPROBE_PATH, '-v', 'error', '-show_entries', 'stream=codec_name', '-of', 'json', str(playback)],
        check=True, capture_output=True, text=True, timeout=10)
    assert [stream['codec_name'] for stream in json.loads(result.stdout)['streams']] == ['h264', 'aac']


def test_fast_subprocess_output_cannot_silently_truncate_timestamp_index(team):
    import sys
    queued_job(team)
    context = JobContext(claim_job())
    with pytest.raises(ProcessingFailure) as error:
        context.run([sys.executable, '-c', 'import sys; sys.stdout.write("0" * (4 * 1024 * 1024 + 1))'], timeout=10)
    assert error.value.code == 'PROCESS_OUTPUT_LIMIT'
