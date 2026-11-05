"""Crash cleanup is bounded and never follows symlinks or reaps live work."""
import os
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from media_pipeline import maintenance
from media_pipeline.models import ProcessingJob
from media_pipeline.worker import claim_job
from visual_drafts.models import FrameEvidence, VisualSession
from .test_media_pipeline import queued_job

pytestmark = pytest.mark.django_db


def old(path):
    stamp = (timezone.now() - timedelta(days=2)).timestamp()
    os.utime(path, (stamp, stamp), follow_symlinks=False)
    return path


def batch(root, *, scans=False, filename=None):
    directory = root / ('temporary-scans' if scans else 'temporary-frames')
    for _ in range(1 if scans else 3):
        directory /= str(uuid.uuid4())
    directory.mkdir(parents=True)
    file = directory / (filename or ('gray.raw' if scans else f'{uuid.uuid4()}.jpg'))
    file.write_bytes(b'owned scratch file')
    old(file)
    old(directory)
    return directory, file


def clean(root):
    return maintenance.cleanup_video_temporary_files(root, now=timezone.now())


def test_idle_tick_reaps_stale_scan_and_frame_batches_without_derived_directory(settings):
    scan, _ = batch(settings.PRIVATE_MEDIA_ROOT, scans=True)
    frames, _ = batch(settings.PRIVATE_MEDIA_ROOT, filename='00.jpg')
    assert not (settings.PRIVATE_MEDIA_ROOT / 'derived').exists()
    maintenance.maintenance_tick()
    assert not scan.exists() and not frames.exists()


def test_recent_directory_or_recent_file_keeps_entire_batch(settings):
    recent_directory, _ = batch(settings.PRIVATE_MEDIA_ROOT, scans=True)
    recent_file_directory, recent_file = batch(settings.PRIVATE_MEDIA_ROOT)
    os.utime(recent_directory, None)
    os.utime(recent_file, None)
    old(recent_file_directory)
    assert clean(settings.PRIVATE_MEDIA_ROOT) == 0
    assert recent_directory.exists() and recent_file.exists()


def test_any_active_unexpired_job_retains_scratch_until_idle(team, settings):
    directory, file = batch(settings.PRIVATE_MEDIA_ROOT, scans=True)
    queued_job(team)
    job = claim_job()
    assert clean(settings.PRIVATE_MEDIA_ROOT) == 0
    assert file.exists()
    ProcessingJob.objects.filter(pk=job.id).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
    assert clean(settings.PRIVATE_MEDIA_ROOT) == 1
    assert not directory.exists()


@pytest.mark.parametrize('field', ['storage_key', 'original_key', 'audio_key', 'thumbnail_key'])
def test_media_reference_preserves_scratch_prefix(team, settings, field):
    directory, file = batch(settings.PRIVATE_MEDIA_ROOT)
    job = queued_job(team)
    setattr(job.asset, field, file.relative_to(settings.PRIVATE_MEDIA_ROOT).as_posix())
    job.asset.save(update_fields=[field])
    assert clean(settings.PRIVATE_MEDIA_ROOT) == 0
    assert directory.exists() and file.exists()


def test_frame_evidence_reference_preserves_scratch_prefix(team, settings):
    directory, file = batch(settings.PRIVATE_MEDIA_ROOT)
    job = queued_job(team)
    session = VisualSession.objects.create(organization=team.org, instruction=job.instruction,
        asset=job.asset, created_by=team.users['author'], mode='upload', consent_images=True,
        base_working_revision=1, language='ru', expires_at=timezone.now() + timedelta(hours=1))
    FrameEvidence.objects.create(organization=team.org, session=session, sequence=1,
        timestamp_ms=0, coverage_start_ms=0, coverage_end_ms=1000, width=64, height=36,
        size_bytes=file.stat().st_size, sha256='a' * 64, request_sha256='a' * 64,
        idempotency_key='cleanup-protection', storage_key=file.relative_to(settings.PRIVATE_MEDIA_ROOT).as_posix())
    assert clean(settings.PRIVATE_MEDIA_ROOT) == 0
    assert directory.exists()


@pytest.mark.parametrize('level', [0, 1, 2, 3])
def test_frame_tree_symlink_at_any_ancestor_is_never_followed(settings, tmp_path, level):
    # Point the selected component outside the temporary tree. The target even
    # has a valid UUID layout, so only no-follow traversal prevents deletion.
    names = ['temporary-frames', *[str(uuid.uuid4()) for _ in range(3)]]
    target = tmp_path / f'outside-{level}'
    target.mkdir()
    leaf = target
    for name in names[level + 1:]:
        leaf /= name
    leaf.mkdir(parents=True, exist_ok=True)
    file = leaf / '00.jpg'
    file.write_bytes(b'must survive')
    old(file)
    old(leaf)
    link = settings.PRIVATE_MEDIA_ROOT.joinpath(*names[:level + 1])
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    assert clean(settings.PRIVATE_MEDIA_ROOT) == 0
    assert link.is_symlink() and file.read_bytes() == b'must survive'


@pytest.mark.parametrize('level', [0, 1])
def test_scan_tree_symlink_is_never_followed(settings, tmp_path, level):
    names = ['temporary-scans', str(uuid.uuid4())]
    target = tmp_path / f'scan-outside-{level}'
    target.mkdir()
    leaf = target / names[1] if level == 0 else target
    leaf.mkdir(exist_ok=True)
    file = leaf / 'gray.raw'
    file.write_bytes(b'must survive')
    old(file)
    old(leaf)
    link = settings.PRIVATE_MEDIA_ROOT.joinpath(*names[:level + 1])
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    assert clean(settings.PRIVATE_MEDIA_ROOT) == 0
    assert file.exists()


def test_unknown_files_subdirectories_and_symlink_files_keep_batches(settings, tmp_path):
    unknown, _ = batch(settings.PRIVATE_MEDIA_ROOT, filename='notes.txt')
    nested, _ = batch(settings.PRIVATE_MEDIA_ROOT)
    (nested / 'nested').mkdir()
    old(nested)
    linked, _ = batch(settings.PRIVATE_MEDIA_ROOT)
    target = tmp_path / 'outside.jpg'
    target.write_bytes(b'not scratch')
    (linked / '01.jpg').symlink_to(target)
    old(linked)
    malformed = settings.PRIVATE_MEDIA_ROOT / 'temporary-scans' / 'not-a-uuid'
    malformed.mkdir(parents=True)
    old(malformed)
    assert clean(settings.PRIVATE_MEDIA_ROOT) == 0
    assert all(path.exists() for path in (unknown, nested, linked, malformed, target))


def test_batch_and_entry_caps_bound_one_cleanup_tick(settings, monkeypatch):
    batches = [batch(settings.PRIVATE_MEDIA_ROOT, scans=True)[0] for _ in range(5)]
    monkeypatch.setattr(maintenance, 'MAX_TEMP_ENTRIES', 1)
    assert clean(settings.PRIVATE_MEDIA_ROOT) == 0
    assert all(path.exists() for path in batches)
    monkeypatch.setattr(maintenance, 'MAX_TEMP_ENTRIES', 500)
    monkeypatch.setattr(maintenance, 'MAX_TEMP_BATCHES', 2)
    assert clean(settings.PRIVATE_MEDIA_ROOT) == 2
    assert sum(path.exists() for path in batches) == 3


def test_excess_files_keep_unknown_batch(settings):
    directory, _ = batch(settings.PRIVATE_MEDIA_ROOT)
    for _ in range(12):
        path = directory / f'{uuid.uuid4()}.jpg'
        path.write_bytes(b'preserve')
        old(path)
    old(directory)
    assert clean(settings.PRIVATE_MEDIA_ROOT) == 0
    assert len(list(directory.iterdir())) == 13
