"""Cheap visual-change hints; these are neither action labels nor evidence timestamps.

A bounded grayscale scan samples the whole prepared video at 5 FPS. A transient
can earn a small burst inside the *existing* image allowance. Uniform anchors
always remain; quiet and steadily changing windows keep the full uniform grid.
The caller maps every selected target back to an actual decoded source PTS.
Events shorter than 200 ms, low-contrast details and hidden actions can be missed.
"""
import bisect
import math
import shutil
import statistics
import time
import uuid
from dataclasses import dataclass
from fractions import Fraction

from PIL import Image, ImageChops

from .exceptions import ProcessingFailure
from .media import binary, local_input
from .storage import make_private_parent, private_path

SCAN_FPS = 5
SCAN_INTERVAL_MS = 1000 // SCAN_FPS
SCAN_WIDTH, SCAN_HEIGHT = 64, 36
SCAN_FRAME_BYTES = SCAN_WIDTH * SCAN_HEIGHT
MAX_SCAN_DURATION_MS = 600000
MAX_SCAN_FRAMES = MAX_SCAN_DURATION_MS * SCAN_FPS // 1000
MAX_SCAN_BYTES = MAX_SCAN_FRAMES * SCAN_FRAME_BYTES
EVENT_SAMPLING_VERSION = 'anchored-transient-burst-v1'
SHORT_EVENT_SAMPLING_VERSION = 'distributed-short-pairs-v2'


@dataclass(frozen=True)
class EventPoint:
    timestamp_ms: int
    score: float
    global_change: bool = False


@dataclass(frozen=True)
class EventScan:
    points: tuple[EventPoint, ...]
    metrics: dict


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProcessingFailure('FRAME_EXTRACTION_TIMEOUT', 'Подготовка кадров превысила допустимое время.')
    return min(60, remaining)


def _change(before, after):
    """Mean grayscale difference with a noise floor and a broad-change flag.

    Broad changes include cuts, lighting and camera movement. This flag can
    deprioritize them; it cannot reliably identify camera motion or occlusion.
    """
    histogram = ImageChops.difference(before, after).histogram()
    changed = sum(histogram[12:])
    score = sum(value * amount for value, amount in enumerate(histogram) if value >= 12)
    return score / (SCAN_FRAME_BYTES * 255), changed / SCAN_FRAME_BYTES >= 0.65


def scan_events(playback, context, *, duration_ms, deadline):
    """One local FFmpeg pass, at most 6.6 MiB temporary pixels, no cloud calls.

    Resampled timestamps are approximate hints. This deliberately does not
    export them as presentation timestamps: the evidence extractor verifies
    the original PTS separately. Every exit removes this scan's own directory.
    """
    if type(duration_ms) is not int or not 0 < duration_ms <= MAX_SCAN_DURATION_MS:
        raise ProcessingFailure('EVENT_SCAN_DURATION_INVALID', 'Недопустимая длительность обзора видео.')
    context.check()
    started = time.monotonic()
    directory = private_path(f'temporary-scans/{uuid.uuid4()}')
    make_private_parent(directory)
    directory.mkdir(mode=0o700)
    path = directory / 'gray.raw'
    frame_limit = min(MAX_SCAN_FRAMES, math.ceil(duration_ms / SCAN_INTERVAL_MS))
    byte_limit = frame_limit * SCAN_FRAME_BYTES
    try:
        context.run([binary('ffmpeg'), '-nostdin', '-hide_banner', '-loglevel', 'error', '-n',
            '-threads', '2', *local_input(playback), '-map', '0:v:0', '-an', '-sn', '-dn',
            '-vf', f'fps=fps={SCAN_FPS}:start_time=0:round=near,scale={SCAN_WIDTH}:{SCAN_HEIGHT}:flags=area,format=gray',
            '-t', f'{duration_ms / 1000:.3f}', '-frames:v', str(frame_limit),
            '-c:v', 'rawvideo', '-threads', '1', '-f', 'rawvideo', '-fs', str(byte_limit), str(path)],
            timeout=_remaining(deadline))
        if not path.is_file() or not 0 < path.stat().st_size <= byte_limit or path.stat().st_size % SCAN_FRAME_BYTES:
            raise ProcessingFailure('EVENT_SCAN_INVALID', 'Не удалось проверить обзор кадров видео.')
        path.chmod(0o600)
        points = []
        previous = None
        with path.open('rb') as source:
            for index in range(frame_limit):
                raw = source.read(SCAN_FRAME_BYTES)
                if not raw:
                    break
                if len(raw) != SCAN_FRAME_BYTES:
                    raise ProcessingFailure('EVENT_SCAN_INVALID', 'Обзор кадров видео оказался неполным.')
                if index % 50 == 0:
                    context.check()
                    _remaining(deadline)
                current = Image.frombytes('L', (SCAN_WIDTH, SCAN_HEIGHT), raw)
                score, broad = _change(previous, current) if previous is not None else (0.0, False)
                points.append(EventPoint(index * SCAN_INTERVAL_MS, round(score, 7), broad))
                previous = current
        context.check()
        _remaining(deadline)
        return EventScan(tuple(points), {
            'scan_fps': SCAN_FPS, 'scan_frames': len(points), 'scan_bytes': path.stat().st_size,
            'scan_ms': round((time.monotonic() - started) * 1000, 3), 'scan_processes': 1,
            'signal': 'grayscale_change', 'signal_timestamps': 'resampled_hints',
        })
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _nearest(target, points, times):
    index = bisect.bisect_left(times, target)
    nearby = [value for value in (index - 1, index) if 0 <= value < len(points)]
    best = min(nearby, key=lambda value: (abs(times[value] - target), times[value]))
    return points[best]


def _uniform(points, times, start_ms, end_ms, count):
    targets = ([(start_ms + end_ms - 1) // 2] if count == 1 else
               [start_ms + ((end_ms - start_ms - 1) * index // (count - 1)) for index in range(count)])
    result = {}
    for target in targets:
        pts = _nearest(target, points, times)
        result.setdefault(pts, target)
    return result


def _fill_gaps(selected, candidates, time_base, limit):
    # VFR can map several requested targets to one PTS. Fill with distinct real
    # points, prioritizing the largest gap, until the existing allowance is used.
    while len(selected) < limit:
        ordered = sorted(selected)
        choices = []
        for left, right in zip(ordered, ordered[1:]):
            available = [pts for pts in candidates if left < pts < right and pts not in selected]
            if available:
                midpoint = Fraction(left + right, 2)
                point = min(available, key=lambda pts: (abs(pts - midpoint), pts))
                choices.append((right - left, left, point))
        if not choices:
            break
        _, _, pts = min(choices, key=lambda row: (-row[0], row[1]))
        selected[pts] = round(pts * time_base * 1000)
    return selected


def _strong_transients(scan, start_ms, end_ms):
    eligible = [point for point in scan.points if start_ms <= point.timestamp_ms < end_ms
                and math.isfinite(point.score) and 0 <= point.score <= 1]
    if not eligible:
        return []
    scores = [point.score for point in eligible]
    median = statistics.median(scores)
    deviation = statistics.median(abs(value - median) for value in scores)
    # A steady sequence is useful context, not a reason to collapse the grid
    # around an arbitrary high-motion frame. Small isolated transients survive.
    threshold = max(0.004, median * 1.6 + 0.001, median + 3 * deviation)
    peaks = [point for point in eligible if point.score >= threshold]
    # Spatially broad changes are lower priority if a local transient exists.
    return sorted(peaks, key=lambda point: (point.global_change, -point.score, point.timestamp_ms))


def select_event_samples(points, time_base, start_ms, end_ms, count, scan):
    """Choose <=count unique actual PTS, in order, preserving uniform anchors.

    For 12 frames: 9 anchors + up to 3 samples around one transient, then fill
    the largest remaining temporal gap. An event burst trades some uniform
    density for local detail: the anchor bound is approximately window/8,
    instead of window/11. Neither policy can establish continuous coverage.
    """
    if (type(count) is not int or not 1 <= count <= 12 or type(start_ms) is not int
            or type(end_ms) is not int or not 0 <= start_ms < end_ms
            or end_ms - start_ms > 60000 or not isinstance(time_base, Fraction) or time_base <= 0):
        raise ProcessingFailure('FRAME_INTERVAL_INVALID', 'Недопустимые параметры выборки кадров.')
    candidates = sorted({pts for pts in points if type(pts) is int
                         and start_ms <= pts * time_base * 1000 < end_ms})
    if not candidates:
        raise ProcessingFailure('NO_VIDEO_FRAMES', 'В выбранном интервале нет кадров видео.')
    times = [pts * time_base * 1000 for pts in candidates]
    limit = min(count, len(candidates))
    peaks = _strong_transients(scan, start_ms, end_ms) if limit >= 6 else []
    if not peaks:
        selected = _uniform(candidates, times, start_ms, end_ms, limit)
        _fill_gaps(selected, candidates, time_base, limit)
        return [(target, pts) for pts, target in sorted(selected.items())], {
            'policy': 'uniform_no_transient', 'anchor_count': len(selected), 'event_targets_ms': [],
            'scan_fps': SCAN_FPS, 'semantic_actions_detected': False,
        }
    anchor_count = limit - (3 if limit >= 9 else 2)
    selected = _uniform(candidates, times, start_ms, end_ms, anchor_count)
    actual_anchor_count = len(selected)
    peak = peaks[0]
    # Record the center before its context if a small frame budget has only
    # two spare slots. The endpoint anchors already preserve video boundaries.
    targets = [peak.timestamp_ms, peak.timestamp_ms - SCAN_INTERVAL_MS, peak.timestamp_ms + SCAN_INTERVAL_MS]
    event_targets = []
    for target in targets:
        if start_ms <= target < end_ms and len(selected) < limit:
            pts = _nearest(target, candidates, times)
            if pts not in selected:
                selected[pts] = target
                event_targets.append(target)
    # Use spare slots for coverage, not additional tightly clustered changes.
    # Source VFR may make midpoint targets collide, so choose an existing PTS
    # in the largest available interval and terminate after at most 12 choices.
    _fill_gaps(selected, candidates, time_base, limit)
    return [(target, pts) for pts, target in sorted(selected.items())], {
        'policy': EVENT_SAMPLING_VERSION, 'anchor_count': actual_anchor_count,
        'event_targets_ms': sorted(event_targets), 'event_center_ms': peak.timestamp_ms,
        'event_global_change': peak.global_change, 'scan_fps': SCAN_FPS,
        'semantic_actions_detected': False,
    }


def _short_episodes(scan, start_ms, end_ms, capacity):
    """Find bounded, separated change intervals, not named actions or cuts.

    Global changes are excluded from both the local baseline and the candidates.
    A broad cut/flash must not suppress a weaker local change or attract a burst.
    Local camera movement can still look like a useful change: no optical-flow
    or semantic detection is claimed by this inexpensive signal.
    """
    eligible = sorted((point for point in scan.points
                       if start_ms <= point.timestamp_ms < end_ms
                       and math.isfinite(point.score) and 0 <= point.score <= 1),
                      key=lambda point: point.timestamp_ms)
    local_scores = [point.score for point in eligible if not point.global_change]
    if not local_scores or not capacity:
        return []
    median = statistics.median(local_scores)
    deviation = statistics.median(abs(value - median) for value in local_scores)
    threshold = max(0.004, median * 1.6 + 0.001, median + 3 * deviation)
    peaks = [index for index, point in enumerate(eligible)
             if not point.global_change and point.score >= threshold]
    candidates = []
    for index in peaks:
        peak = eligible[index]
        first = max(start_ms, peak.timestamp_ms - SCAN_INTERVAL_MS)
        last = min(end_ms - 1, peak.timestamp_ms + SCAN_INTERVAL_MS)
        # Do not build a motion-looking pair across a broad
        # change. Uniform anchors will still retain the scene's context.
        if any(point.global_change and first < point.timestamp_ms <= last for point in eligible):
            continue
        if first < peak.timestamp_ms < last:
            candidates.append({'start_ms': first, 'peak_ms': peak.timestamp_ms,
                               'end_ms': last, 'score': peak.score})
    selected = []
    duration = end_ms - start_ms
    separation = max(3 * SCAN_INTERVAL_MS, min(2500, duration // 5))
    while candidates and len(selected) < capacity:
        # Favor an unrepresented temporal third after the strongest candidate.
        # This prevents several peaks of one long motion consuming all extras.
        bucket = lambda value: min(2, (value['peak_ms'] - start_ms) * 3 // duration)
        buckets = {bucket(value) for value in selected}
        candidate = min(candidates, key=lambda value: (bucket(value) in buckets,
                                                      -value['score'], value['peak_ms']))
        selected.append(candidate)
        candidates = [value for value in candidates
                      if abs(value['peak_ms'] - candidate['peak_ms']) >= separation
                      and (value['end_ms'] < candidate['start_ms']
                           or value['start_ms'] > candidate['end_ms'])]
    return sorted(selected, key=lambda value: value['peak_ms'])


def select_short_event_samples(points, time_base, start_ms, end_ms, count, scan):
    """At most two short pairs within the same twelve-image allowance.

    Twelve images can contain eight uniform anchors plus two local pairs.
    With one useful episode there are count-2 anchors. With fewer than nine images,
    no local change, only broad changes or steady motion, use the full grid.
    Eight anchors keep the base temporal gap to about window/7, rather than the
    older event policy's window/8; real VFR gaps can be larger. These pairs do
    not prove an action or supply separate before/during/after observations.
    """
    if (type(count) is not int or not 1 <= count <= 12 or type(start_ms) is not int
            or type(end_ms) is not int or not 0 <= start_ms < end_ms
            or end_ms - start_ms > 60000 or not isinstance(time_base, Fraction) or time_base <= 0):
        raise ProcessingFailure('FRAME_INTERVAL_INVALID', 'Недопустимые параметры выборки кадров.')
    candidates = sorted({pts for pts in points if type(pts) is int
                         and start_ms <= pts * time_base * 1000 < end_ms})
    if not candidates:
        raise ProcessingFailure('NO_VIDEO_FRAMES', 'В выбранном интервале нет кадров видео.')
    times = [pts * time_base * 1000 for pts in candidates]
    limit = min(count, len(candidates))
    capacity = 2 if limit == 12 else 1 if limit >= 9 else 0
    episodes = _short_episodes(scan, start_ms, end_ms, capacity)
    groups = []
    for episode in episodes:
        targets = [episode['start_ms'], episode['end_ms']]
        mapped = [_nearest(target, candidates, times) for target in targets]
        # Sparse/VFR sources must actually contain two distinct pictures;
        # nearby requested times do not themselves constitute a motion pair.
        if len(set(mapped)) == 2:
            groups.append((episode, targets, mapped))
    anchor_count = limit - 2 * len(groups)
    selected = _uniform(candidates, times, start_ms, end_ms, anchor_count)
    actual_anchors = len(selected)
    event_targets, metadata_groups = [], []
    for episode, targets, mapped in groups:
        for target, pts in zip(targets, mapped):
            if pts not in selected:
                selected[pts] = target
                event_targets.append(target)
        metadata_groups.append({
            'start_ms': episode['start_ms'], 'peak_ms': episode['peak_ms'], 'end_ms': episode['end_ms'],
            'target_times_ms': targets,
            'selected_timestamps_ms': [round(pts * time_base * 1000000) // 1000 for pts in mapped],
        })
    _fill_gaps(selected, candidates, time_base, limit)
    return [(target, pts) for pts, target in sorted(selected.items())], {
        'policy': SHORT_EVENT_SAMPLING_VERSION if groups else 'uniform_no_short_transient',
        'anchor_count': actual_anchors, 'event_targets_ms': sorted(event_targets),
        'series_kind': 'short_pairs', 'event_groups': metadata_groups, 'scan_fps': SCAN_FPS,
        'semantic_actions_detected': False,
    }
