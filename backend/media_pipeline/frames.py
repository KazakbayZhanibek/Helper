"""Bounded visual evidence from a ready private playback, never an action detector.

Use ``with extract_video_frames(asset, context, start_ms=..., end_ms=...) as batch``.
Frames are attempt-local. Copy a frame into a separately owned persistent location
before saving a database reference; this helper always owns and cleans its files.
``timestamp_us`` is a decoded presentation timestamp, not the requested seek time.
Sparse samples cannot establish that an action occurred between sampled frames.
"""
import bisect
import hashlib
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from django.conf import settings

from core.models import MediaAsset
from .exceptions import ProcessingFailure
from .media import binary, local_input
from .models import ProcessingJob
from .storage import private_path, make_private_parent

MAX_FRAMES = 12
MAX_FRAME_SIDE = 960
MAX_INTERVAL_MS = 60000
MAX_FRAME_BYTES = 1024 * 1024
MAX_BATCH_BYTES = 8 * 1024 * 1024
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_PACKET_COUNT = 200000
MAX_PLAYBACK_BYTES = 750 * 1024 * 1024
MAX_BATCH_SECONDS = 120
SAMPLING_VERSION = 'event-grid-decoded-pts-v2'
STRATEGIES = frozenset({'events', 'short_events', 'uniform', 'uniform_legacy'})


@dataclass(frozen=True)
class VideoFrame:
    id: str
    path: Path
    storage_key: str
    requested_timestamp_ms: int
    timestamp_ms: int
    timestamp_us: int
    pts: int
    time_base: str
    width: int
    height: int
    size_bytes: int
    sha256: str

    def metadata(self):
        return {key: getattr(self, key) for key in ('id', 'requested_timestamp_ms', 'timestamp_ms',
            'timestamp_us', 'pts', 'time_base', 'width', 'height', 'size_bytes', 'sha256')}


class FrameBatch:
    """Private temporary frame ownership. Manifest deliberately contains no paths or URLs."""
    def __init__(self, *, directory, frames, source, playback_sha256, start_ms, end_ms, requested_count,
                 sampling_version=SAMPLING_VERSION, processing=None, selection=None):
        self.id = directory.name
        self.directory = directory
        self.frames = tuple(frames)
        self.asset_id = str(source.id)
        self.source_sha256 = source.sha256
        self.playback_sha256 = playback_sha256
        self.start_ms, self.end_ms = start_ms, end_ms
        self.requested_count = requested_count
        self.sampling_version = sampling_version
        self.processing = dict(processing or {})
        self.selection = dict(selection or {})
        self.closed = False

    def manifest(self):
        times = [frame.timestamp_us for frame in self.frames]
        boundaries = [self.start_ms * 1000, *times, self.end_ms * 1000]
        return {
            'batch_id': self.id, 'asset_id': self.asset_id, 'source_sha256': self.source_sha256,
            'playback_sha256': self.playback_sha256, 'sampling_version': self.sampling_version,
            'start_ms': self.start_ms, 'end_ms': self.end_ms, 'requested_count': self.requested_count,
            'frame_count': len(self.frames), 'frames': [frame.metadata() for frame in self.frames],
            'sample_gaps_us': [right - left for left, right in zip(times, times[1:])],
            'largest_unsampled_interval_us': max(right - left for left, right in zip(boundaries, boundaries[1:])),
            'sparse_sampling': True,
            'processing': self.processing, 'selection': self.selection,
        }

    def cleanup(self):
        if not self.closed:
            shutil.rmtree(self.directory, ignore_errors=True)
            self.closed = True

    def __enter__(self):
        if self.closed:
            raise RuntimeError('Frame batch has already been cleaned up')
        return self

    def __exit__(self, *args):
        self.cleanup()


def _failure(code, message):
    raise ProcessingFailure(code, message)


def _ready_source(asset, context):
    context.check()
    job = ProcessingJob.objects.filter(pk=context.job_id).only('asset_id', 'organization_id', 'instruction_id').first()
    if not job or job.asset_id != asset.id or job.organization_id != asset.organization_id:
        _failure('FRAME_SOURCE_MISMATCH', 'Задание не относится к этому видео.')
    current = MediaAsset.objects.filter(pk=asset.id, organization_id=job.organization_id, status='ready').first()
    if not current or not current.storage_key or not current.duration_ms:
        _failure('MEDIA_NOT_READY', 'Дождитесь подготовки видео перед анализом кадров.')
    return current


def _hash_file(path, context, deadline):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            context.check()
            _remaining(deadline)
            digest.update(chunk)
    return digest.hexdigest()


def _remaining(deadline, *, cap=20):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _failure('FRAME_EXTRACTION_TIMEOUT', 'Извлечение кадров превысило допустимое время.')
    return min(cap, remaining)


def _packet_index(source, context, deadline):
    result = context.run([binary('ffprobe'), '-v', 'error', *local_input(source), '-select_streams', 'v:0',
        '-show_entries', 'stream=time_base:packet=pts', '-of', 'json=compact=1'], timeout=_remaining(deadline))
    if len(result.stdout.encode('utf-8')) > MAX_INDEX_BYTES:
        _failure('FRAME_INDEX_LIMIT', 'Временной индекс видео превышает допустимый размер.')
    try:
        data = json.loads(result.stdout)
        streams, packets = data['streams'], data['packets']
        if len(streams) != 1 or not isinstance(packets, list) or not 0 < len(packets) <= MAX_PACKET_COUNT:
            raise ValueError()
        time_base = Fraction(streams[0]['time_base'])
        if time_base <= 0 or time_base > 1:
            raise ValueError()
        pts = sorted({packet['pts'] for packet in packets if type(packet.get('pts')) is int and packet['pts'] >= 0})
        if not pts:
            raise ValueError()
        return time_base, pts
    except (KeyError, TypeError, ValueError, ZeroDivisionError, AttributeError):
        _failure('FRAME_TIMING_INVALID', 'Не удалось проверить временные метки кадров.')


def _select_samples(points, time_base, start_ms, end_ms, count):
    candidates = [pts for pts in points if start_ms <= pts * time_base * 1000 < end_ms]
    if not candidates:
        _failure('NO_VIDEO_FRAMES', 'В выбранном интервале нет кадров видео.')
    count = min(count, len(candidates))
    if count == 1:
        targets = [(start_ms + end_ms - 1) // 2]
    else:
        targets = [start_ms + ((end_ms - start_ms - 1) * index // (count - 1)) for index in range(count)]
    times = [pts * time_base * 1000 for pts in candidates]
    samples, seen = [], set()
    for target in targets:
        insertion = bisect.bisect_left(times, target)
        choices = [index for index in (insertion - 1, insertion) if 0 <= index < len(candidates)]
        best = min(choices, key=lambda index: (abs(times[index] - target), times[index]))
        pts = candidates[best]
        if pts not in seen:
            seen.add(pts)
            samples.append((target, pts))
    return samples


def _required_points(required_frames, points, time_base, start_ms, end_ms, count):
    """Bind parent sample times to this playback's exact packet clock.

    Callers must establish parent/source ownership separately. These references
    may use a different rational clock but must resolve to existing native PTS;
    rounding a near match could silently replace a short-action anchor.
    """
    try:
        if not isinstance(required_frames, (list, tuple)) or len(required_frames) > MAX_FRAMES:
            raise ValueError()
        if not required_frames:
            return []
        available, required = set(points), set()
        for frame in required_frames:
            if not isinstance(frame, dict):
                raise ValueError()
            pts, raw_base = frame['pts'], frame['time_base']
            if (type(pts) is not int or not 0 <= pts <= 2 ** 63 - 1
                    or not isinstance(raw_base, str) or len(raw_base) > 64
                    or not re.fullmatch(r'[1-9]\d*(?:/[1-9]\d*)?', raw_base)):
                raise ValueError()
            base = Fraction(raw_base)
            if not 0 < base <= 1:
                raise ValueError()
            native = pts * base / time_base
            if (native.denominator != 1 or native.numerator not in available
                    or not start_ms <= pts * base * 1000 < end_ms):
                raise ValueError()
            required.add(native.numerator)
        if len(required) > count:
            raise ValueError()
        return sorted(required)
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        _failure('FRAME_REFINEMENT_INVALID', 'Не удалось сохранить исходные кадры при подробном просмотре.')


def _select_anchored_samples(points, time_base, start_ms, end_ms, count, required):
    """Retain trusted anchors, then endpoints, then bisect the largest gaps.

    Boundary context uses the same spare slots as interior samples. If only one
    slot remains, prefer the endpoint farther from its nearest protected anchor;
    ties go to the earlier PTS. Exact duplicate points consume no slot. No
    time-distance heuristic calls two distinct pictures interchangeable.
    Endpoints never displace a required image, even when those fill the budget.
    """
    candidates = [pts for pts in points if start_ms <= pts * time_base * 1000 < end_ms]
    if not candidates:
        _failure('NO_VIDEO_FRAMES', 'В выбранном интервале нет кадров видео.')
    selected = set(required)
    count = min(count, len(candidates))
    boundaries = set((candidates[0], candidates[-1])) - selected
    # Score against the protected anchors before adding either endpoint. This
    # keeps a one-slot decision deterministic and independent of input order.
    boundary_order = sorted(boundaries, key=lambda pts: (
        -min((abs(pts - anchor) for anchor in required), default=0), pts))
    for pts in boundary_order:
        if len(selected) >= count:
            break
        selected.add(pts)
    left_edge = Fraction(start_ms, 1000) / time_base
    right_edge = Fraction(end_ms, 1000) / time_base
    while len(selected) < count:
        boundaries = [left_edge, *sorted(selected), right_edge]
        gaps = []
        for index, (left, right) in enumerate(zip(boundaries, boundaries[1:])):
            lo = bisect.bisect_left(candidates, left) if index == 0 else bisect.bisect_right(candidates, left)
            hi = bisect.bisect_left(candidates, right)
            if lo < hi:
                gaps.append((right - left, -left, lo, hi, (left + right) / 2))
        if not gaps:
            _failure('FRAME_SELECTION_INVALID', 'Не удалось дополнить исходные кадры подробным просмотром.')
        _, _, lo, hi, midpoint = max(gaps)
        insertion = bisect.bisect_left(candidates, midpoint, lo, hi)
        options = [index for index in (insertion - 1, insertion) if lo <= index < hi]
        selected.add(candidates[min(options, key=lambda index: (abs(candidates[index] - midpoint), candidates[index]))])
    return [(int(pts * time_base * 1000), pts) for pts in sorted(selected)]


def _decoded_metadata(stderr, expected_pts, expected_base):
    base_match = re.search(r'\[Parsed_showinfo_\d+[^\]]*\]\s+config in time_base:\s*(\d+/\d+)', stderr)
    frame_match = re.search(r'\[Parsed_showinfo_\d+[^\]]*\]\s+n:\s*0\s+pts:\s*(-?\d+)\s+pts_time:[^\n]*?\bs:(\d+)x(\d+)\b', stderr)
    if not base_match or not frame_match:
        _failure('FRAME_TIMING_INVALID', 'Не удалось проверить время извлечённого кадра.')
    try:
        time_base = Fraction(base_match[1])
        pts, width, height = (int(frame_match[index]) for index in (1, 2, 3))
        if pts * time_base != expected_pts * expected_base or not 1 <= min(width, height) <= max(width, height) <= MAX_FRAME_SIDE:
            raise ValueError()
    except (ValueError, ZeroDivisionError):
        _failure('FRAME_TIMING_INVALID', 'Извлечённый кадр не соответствует выбранному времени.')
    return pts, time_base, width, height


def _file_identity(source, playback):
    try:
        stat = playback.stat()
        if not playback.is_file() or not 0 < stat.st_size <= MAX_PLAYBACK_BYTES:
            raise OSError()
    except OSError:
        _failure('FRAME_SOURCE_UNAVAILABLE', 'Подготовленное видео недоступно или превышает лимит.')
    # ctime detects in-place writes even when size and mtime are restored. The
    # cache lives only on this job attempt, never in a process-global dictionary.
    return (str(source.id), str(source.organization_id), source.sha256, source.storage_key,
            source.duration_ms, str(playback), stat.st_dev, stat.st_ino, stat.st_size,
            stat.st_mtime_ns, stat.st_ctime_ns)


def _assert_source(source, context, identity):
    current = _ready_source(source, context)
    if _file_identity(current, private_path(current.storage_key)) != identity:
        _failure('FRAME_SOURCE_CHANGED', 'Источник видео изменился во время извлечения кадров.')


def _elapsed_ms(started):
    return round((time.monotonic() - started) * 1000, 3)


def _source_index(source, playback, context, deadline, identity, metrics, *, cache_enabled):
    owner = (str(context.job_id), str(getattr(context, 'lease_token', '')))
    fence = getattr(context, '_video_source_fence', None)
    if fence and fence[0] == owner and fence[1] != identity:
        _failure('FRAME_SOURCE_CHANGED', 'Источник видео изменился во время анализа.')
    cache = getattr(context, '_video_source_index', None) if cache_enabled else None
    if cache and cache['owner'] == owner:
        if cache['identity'] != identity:
            # A ready playback is immutable. Do not silently reuse an old event
            # scan, or mix evidence from two versions of a file in one attempt.
            _failure('FRAME_SOURCE_CHANGED', 'Источник видео изменился во время анализа.')
        metrics['index_cache_hit'] = True
        return cache
    started = time.monotonic()
    digest = _hash_file(playback, context, deadline)
    metrics['hash_ms'] = _elapsed_ms(started)
    started = time.monotonic()
    time_base, points = _packet_index(playback, context, deadline)
    metrics['index_ms'], metrics['ffprobe_processes'] = _elapsed_ms(started), 1
    _assert_source(source, context, identity)
    context._video_source_fence = (owner, identity)
    cache = {'owner': owner, 'identity': identity, 'playback_sha256': digest,
             'time_base': time_base, 'points': points, 'events': None}
    if cache_enabled:
        context._video_source_index = cache
    return cache


def _decoded_batch_metadata(stderr, samples, expected_base):
    bases = re.findall(r'\[Parsed_showinfo_\d+[^\]]*\]\s+config in time_base:\s*(\d+/\d+)', stderr)
    records = re.findall(r'\[Parsed_showinfo_\d+[^\]]*\]\s+n:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:[^\n]*?\bs:(\d+)x(\d+)\b', stderr)
    try:
        if len(bases) != 1 or len(records) != len(samples):
            raise ValueError()
        base = Fraction(bases[0])
        result = []
        for index, (record, (_, expected_pts)) in enumerate(zip(records, samples)):
            number, pts, width, height = map(int, record)
            if (number != index or pts * base != expected_pts * expected_base
                    or not 1 <= min(width, height) <= max(width, height) <= MAX_FRAME_SIDE):
                raise ValueError()
            result.append((pts, base, width, height))
        return result
    except (ValueError, ZeroDivisionError):
        _failure('FRAME_TIMING_INVALID', 'Извлечённые кадры не соответствуют выбранному времени.')


def _decode_frames(playback, context, samples, time_base, directory, deadline, *, legacy):
    """Decode only selected pictures at full output resolution, in PTS order.

    The normal path starts one decoder for the window, avoiding one seek/process
    per image. The old path is retained solely as an explicit comparison/rollback.
    """
    scale = f"scale=w='min({MAX_FRAME_SIDE},iw)':h='min({MAX_FRAME_SIDE},ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
    metadata = []
    groups = [[sample] for sample in samples] if legacy else [samples]
    for index, group in enumerate(groups):
        context.check()
        seek_us = max(0, group[0][1] * time_base * 1000000 // 1 - 1000)
        seek = f'{seek_us // 1000000}.{seek_us % 1000000:06d}'
        expression = '+'.join(f'eq(pts\\,{pts})' for _, pts in group)
        output = directory / (f'{index:02d}.jpg' if legacy else '%02d.jpg')
        options = ['-update', '1'] if legacy else ['-start_number', '0']
        result = context.run([binary('ffmpeg'), '-nostdin', '-hide_banner', '-loglevel', 'info', '-n', '-threads', '2',
            '-ss', seek, '-copyts', *local_input(playback), '-map', '0:v:0', '-an', '-sn', '-dn',
            '-vf', f'select={expression},{scale},showinfo', '-frames:v', str(len(group)), '-c:v', 'mjpeg', '-threads', '1',
            '-q:v', '3', '-map_metadata', '-1', '-fps_mode', 'passthrough', *options,
            '-fs', str(MAX_FRAME_BYTES if legacy else MAX_BATCH_BYTES), str(output)],
            timeout=_remaining(deadline, cap=20 if legacy else 60))
        metadata.extend(_decoded_batch_metadata(result.stderr, group, time_base))
    return metadata


def extract_video_frames(asset, context, *, start_ms=0, end_ms=None, count=MAX_FRAMES, strategy=None, required_frames=()):
    """At most 12 verified JPEG evidence points from an explicit ≤60s interval.

    Event selection keeps a temporal grid within the same image allowance.
    ``uniform`` uses the new batch decoder with the previous selection, while
    ``uniform_legacy`` also restores per-image decoders for repeatable comparison.
    Hash/index/event data is reused only within the current fenced job attempt.
    ``required_frames`` preserves trusted parent PTS in a refinement, filling the
    remaining allowance with actual frames from the largest temporal gaps.
    """
    started = time.monotonic()
    if strategy is None:
        strategy = getattr(settings, 'VIDEO_FRAME_STRATEGY', 'events')
    source = _ready_source(asset, context)
    if end_ms is None:
        end_ms = source.duration_ms
    if (type(start_ms) is not int or type(end_ms) is not int or type(count) is not int
            or not 1 <= count <= MAX_FRAMES or not 0 <= start_ms < end_ms <= source.duration_ms
            or end_ms - start_ms > MAX_INTERVAL_MS):
        _failure('FRAME_INTERVAL_INVALID', 'Выберите интервал до 60 секунд и не больше 12 кадров.')
    if not isinstance(strategy, str) or strategy not in STRATEGIES:
        _failure('FRAME_STRATEGY_INVALID', 'Неизвестный способ выбора кадров.')
    playback = private_path(source.storage_key)
    identity = _file_identity(source, playback)
    deadline = started + MAX_BATCH_SECONDS
    metrics = {'strategy': strategy, 'index_cache_hit': False, 'event_cache_hit': False,
               'hash_ms': 0, 'index_ms': 0, 'event_scan_ms': 0, 'selection_ms': 0,
               'decode_ms': 0, 'total_ms': 0, 'ffmpeg_processes': 0, 'ffprobe_processes': 0}
    cache = _source_index(source, playback, context, deadline, identity, metrics,
                          cache_enabled=strategy != 'uniform_legacy')
    time_base, points = cache['time_base'], cache['points']
    required = _required_points(required_frames, points, time_base, start_ms, end_ms, count)
    selection = {'policy': 'uniform', 'event_targets_ms': [], 'semantic_actions_detected': False}
    if required:
        selection_started = time.monotonic()
        samples = _select_anchored_samples(points, time_base, start_ms, end_ms, count, required)
        selection.update(policy='anchored_refinement', anchor_count=len(required),
                         required_points=[{'pts': pts, 'time_base': str(time_base)} for pts in required])
        candidates = [pts for pts in points if start_ms <= pts * time_base * 1000 < end_ms]
        selected_points = {pts for _, pts in samples}
        selection.update(boundary_policy='spare_slots_farthest_endpoint_first',
                         boundary_samples=[{
                             'edge': edge, 'pts': pts, 'time_base': str(time_base),
                             'selected': pts in selected_points, 'required': pts in required,
                         } for edge, pts in (('start', candidates[0]), ('end', candidates[-1]))])
    elif (strategy == 'events' and count >= 6) or (strategy == 'short_events' and count >= 9):
        from .event_sampling import scan_events, select_event_samples, select_short_event_samples
        metrics['event_cache_hit'] = cache['events'] is not None
        if cache['events'] is None:
            scan_started = time.monotonic()
            cache['events'] = scan_events(playback, context, duration_ms=source.duration_ms, deadline=deadline)
            metrics['event_scan_ms'] = _elapsed_ms(scan_started)
            metrics['ffmpeg_processes'] += 1
            _assert_source(source, context, identity)
        selection_started = time.monotonic()
        selector = select_short_event_samples if strategy == 'short_events' else select_event_samples
        samples, selection = selector(points, time_base, start_ms, end_ms, count, cache['events'])
    else:
        selection_started = time.monotonic()
        samples = _select_samples(points, time_base, start_ms, end_ms, count)
    metrics['selection_ms'] = _elapsed_ms(selection_started)
    # Selection is an internal optimization; it must never enlarge the model
    # request, reorder frames or change the exact-PTS evidence contract.
    if (not 1 <= len(samples) <= count or len({pts for _, pts in samples}) != len(samples)
            or samples != sorted(samples, key=lambda sample: sample[1])
            or any(type(pts) is not int or pts not in points
                   or not start_ms <= pts * time_base * 1000 < end_ms for _, pts in samples)):
        _failure('FRAME_SELECTION_INVALID', 'Не удалось проверить выборку кадров.')
    _assert_source(source, context, identity)
    batch_key = f'temporary-frames/{source.organization_id}/{source.id}/{uuid.uuid4()}'
    directory = private_path(batch_key)
    make_private_parent(directory)
    directory.mkdir(mode=0o700)
    frames, total_bytes = [], 0
    try:
        decode_started = time.monotonic()
        decoded = _decode_frames(playback, context, samples, time_base, directory, deadline,
                                 legacy=strategy == 'uniform_legacy')
        metrics['decode_ms'] = _elapsed_ms(decode_started)
        metrics['ffmpeg_processes'] += len(samples) if strategy == 'uniform_legacy' else 1
        for index, ((target, _), (pts, base, width, height)) in enumerate(zip(samples, decoded)):
            context.check()
            _remaining(deadline)
            temporary = directory / f'{index:02d}.jpg'
            if not temporary.is_file() or not 0 < temporary.stat().st_size <= MAX_FRAME_BYTES:
                _failure('FRAME_SIZE_LIMIT', 'Кадр превышает допустимый размер.')
            size = temporary.stat().st_size
            total_bytes += size
            if total_bytes > MAX_BATCH_BYTES:
                _failure('FRAME_BATCH_SIZE_LIMIT', 'Набор кадров превышает допустимый размер.')
            data = temporary.read_bytes()
            if not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
                _failure('FRAME_ENCODING_INVALID', 'Не удалось сохранить полный JPEG кадра.')
            frame_id = uuid.uuid4()
            key = f'{batch_key}/{frame_id}.jpg'
            path = private_path(key)
            temporary.rename(path)
            path.chmod(0o600)
            timestamp_us = round(pts * base * 1000000)
            frames.append(VideoFrame(str(frame_id), path, key, target, timestamp_us // 1000, timestamp_us,
                pts, str(base), width, height, size, hashlib.sha256(data).hexdigest()))
        _assert_source(source, context, identity)
        _remaining(deadline)
        metrics['total_ms'] = _elapsed_ms(started)
        version = ('anchored-refinement-decoded-pts-v2' if required else
                   ('short-pairs-decoded-pts-v2' if strategy == 'short_events' else SAMPLING_VERSION) if selection.get('event_targets_ms')
                   else 'decoded-pts-nearest-time-v1')
        return FrameBatch(directory=directory, frames=frames, source=source, playback_sha256=cache['playback_sha256'],
            start_ms=start_ms, end_ms=end_ms, requested_count=count, sampling_version=version,
            processing=metrics, selection=selection)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
