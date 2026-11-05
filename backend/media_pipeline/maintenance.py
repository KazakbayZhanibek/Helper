"""Small periodic cleanup; never delete a referenced file or a live worker attempt."""
import os
import re
import shutil
import stat
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import MediaAsset, Organization
from .models import UploadSession, ProcessingJob
from .uploads import expire_uploads

MAX_TEMP_ENTRIES = 500
MAX_TEMP_BATCHES = 50
MAX_TEMP_FILES = 12


def _uuid_name(name):
    try:
        return str(uuid.UUID(name)) == name
    except (ValueError, AttributeError):
        return False


def _open_directory(name, *, parent=None):
    # Every tree component is opened relative to an already open directory.
    # O_NOFOLLOW covers ancestors as well as the final batch; a symlink is never
    # resolved, even if a directory entry changes between enumeration and open.
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except OSError:
        return None


def cleanup_video_temporary_files(root, *, now):
    """Reap bounded, known, flat scratch batches after 24 hours of inactivity.

    Batch IDs are not lease tokens. Conservatively retain all scratch files
    while any unexpired job is running. This runs between jobs on the current
    single-worker deployment; a new attempt always creates a fresh UUID batch.
    Unknown layouts and any recent or referenced file keep the whole batch.
    """
    from visual_drafts.models import FrameEvidence

    def active_job():
        return ProcessingJob.objects.filter(state='running', lease_expires_at__gt=now).exists()

    if active_job():
        return 0
    root_fd = _open_directory(root)
    if root_fd is None:
        return 0
    cutoff = (now - timedelta(hours=24)).timestamp()
    visited, removed = 0, 0

    def remove_batch(parent_fd, name, batch_fd, relative, scans):
        nonlocal visited, removed
        info = os.fstat(batch_fd)
        if info.st_mtime >= cutoff:
            return
        files = []
        with os.scandir(batch_fd) as entries:
            for entry in entries:
                visited += 1
                if visited > MAX_TEMP_ENTRIES or len(files) >= MAX_TEMP_FILES:
                    return
                valid_name = entry.name == 'gray.raw' if scans else (
                    entry.name.endswith('.jpg') and (_uuid_name(entry.name[:-4])
                    or re.fullmatch(r'(?:0[0-9]|1[01])\.jpg', entry.name) is not None))
                item = entry.stat(follow_symlinks=False)
                if not valid_name or not stat.S_ISREG(item.st_mode) or item.st_mtime >= cutoff:
                    return
                files.append((entry.name, item.st_dev, item.st_ino))
        prefix = relative + '/'
        if (active_job() or MediaAsset.objects.filter(Q(storage_key__startswith=prefix)
                | Q(original_key__startswith=prefix) | Q(audio_key__startswith=prefix)
                | Q(thumbnail_key__startswith=prefix)).exists()
                or FrameEvidence.objects.filter(storage_key__startswith=prefix).exists()):
            return
        # Refuse a replaced batch, including a symlink introduced after opening.
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            return
        for filename, device, inode in files:
            current = os.stat(filename, dir_fd=batch_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (device, inode) or not stat.S_ISREG(current.st_mode):
                return
        for filename, _, _ in files:
            os.unlink(filename, dir_fd=batch_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
            os.rmdir(name, dir_fd=parent_fd)
            removed += 1

    def walk(parent_fd, relative, levels, scans):
        nonlocal visited
        with os.scandir(parent_fd) as entries:
            for entry in entries:
                if visited >= MAX_TEMP_ENTRIES or removed >= MAX_TEMP_BATCHES:
                    return
                visited += 1
                if not _uuid_name(entry.name):
                    continue
                child_fd = _open_directory(entry.name, parent=parent_fd)
                if child_fd is None:
                    continue
                try:
                    child_relative = relative + '/' + entry.name
                    if levels == 1:
                        remove_batch(parent_fd, entry.name, child_fd, child_relative, scans)
                    else:
                        walk(child_fd, child_relative, levels - 1, scans)
                except OSError:
                    # A concurrent cleanup/removal is harmless; never descend
                    # recursively into an unexpected layout to force deletion.
                    continue
                finally:
                    os.close(child_fd)

    try:
        for prefix, depth in (('temporary-scans', 1), ('temporary-frames', 3)):
            directory_fd = _open_directory(prefix, parent=root_fd)
            if directory_fd is None:
                continue
            try:
                walk(directory_fd, prefix, depth, prefix == 'temporary-scans')
            finally:
                os.close(directory_fd)
    finally:
        os.close(root_fd)
    return removed


def maintenance_tick():
    from visual_drafts.cleanup import cleanup_visual_sessions

    cleanup_visual_sessions(limit=50)
    now = timezone.now()
    org_ids = UploadSession.objects.filter(state__in=['pending', 'uploading'], expires_at__lte=now).values_list('organization_id', flat=True).distinct()
    for org_id in list(org_ids[:100]):
        with transaction.atomic():
            organization = Organization.objects.select_for_update().get(pk=org_id)
            expire_uploads(organization)
    # A crashed request/process can leave a partial private file. Keep a full day
    # before removing unreferenced paths, as required by the retention contract.
    cutoff = (now - timedelta(hours=24)).timestamp()
    root = Path(settings.PRIVATE_MEDIA_ROOT).resolve()
    if not root.is_dir():
        return
    cleanup_video_temporary_files(root, now=now)
    for upload in UploadSession.objects.filter(asset__isnull=True, state__in=['failed', 'cancelled', 'expired'], updated_at__lt=now - timedelta(hours=24))[:100]:
        directory = root / 'quarantine' / str(upload.organization_id) / str(upload.id)
        if directory.is_dir() and not directory.is_symlink() and directory.resolve().is_relative_to(root):
            shutil.rmtree(directory)
    derived = root / 'derived'
    if not derived.is_dir():
        return
    # Fixed-depth UUID tree: no user-controlled directory names and no recursive symlink traversal.
    for directory in derived.glob('*/*/*'):
        if not directory.is_dir() or directory.is_symlink() or not directory.resolve().is_relative_to(root) or directory.stat().st_mtime >= cutoff:
            continue
        key_prefix = directory.relative_to(root).as_posix() + '/'
        if MediaAsset.objects.filter(storage_key__startswith=key_prefix).exists():
            continue
        try:
            import uuid
            token = uuid.UUID(directory.name)
        except ValueError:
            continue
        if ProcessingJob.objects.filter(lease_token=token, state='running', lease_expires_at__gt=now).exists():
            continue
        shutil.rmtree(directory)
