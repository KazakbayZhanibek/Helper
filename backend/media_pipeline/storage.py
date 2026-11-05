import os
from pathlib import Path

from django.conf import settings

from .exceptions import ProcessingFailure


def private_path(key):
    root = Path(settings.PRIVATE_MEDIA_ROOT).resolve()
    candidate = (root / key).resolve()
    if not key or candidate == root or not candidate.is_relative_to(root):
        raise ProcessingFailure('INVALID_STORAGE_PATH', 'Недопустимый путь файла.')
    return candidate


def make_private_parent(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def private_open(path):
    make_private_parent(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, 'wb')
