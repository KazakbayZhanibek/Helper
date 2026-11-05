"""Sampling fixtures prove visible-change coverage, not recognition of actions."""
import subprocess
import time
from fractions import Fraction
from types import SimpleNamespace

import pytest

from media_pipeline import event_sampling as events
from media_pipeline.exceptions import Cancelled, ProcessingFailure
from .test_media_pipeline import installed_media_binary


class LocalContext:
    def __init__(self):
        self.calls = []

    def check(self):
        pass

    def run(self, argv, timeout):
        self.calls.append(argv)
        return subprocess.run(argv, timeout=timeout, check=True, capture_output=True, text=True)


def scan(points):
    return events.EventScan(tuple(events.EventPoint(*point) for point in points), {})


def grid(end_ms=30000):
    return list(range(0, end_ms, 40)), Fraction(1, 1000)


def test_static_and_continuous_changes_keep_full_uniform_grid():
    points, base = grid()
    for score in (0, 0.02):
        value = scan([(stamp, score, False) for stamp in range(0, 30000, 200)])
        chosen, metadata = events.select_event_samples(points, base, 0, 30000, 12, value)
        assert len(chosen) == 12
        assert metadata['policy'] == 'uniform_no_transient'
        times = [pts for _, pts in chosen]
        assert times[0] == 0 and times[-1] == 29960
        assert max(b - a for a, b in zip(times, times[1:])) <= 2760


def test_brief_local_event_gets_before_during_after_within_same_budget():
    points, base = grid()
    value = scan([(stamp, 0.1 if stamp == 1200 else 0.0, False) for stamp in range(0, 30000, 200)])
    chosen, metadata = events.select_event_samples(points, base, 0, 30000, 12, value)
    times = [pts for _, pts in chosen]
    assert len(times) == len(set(times)) == 12
    assert times == sorted(times)
    assert {1000, 1200, 1400} <= set(times)
    assert metadata['anchor_count'] == 9
    assert times[0] == 0 and times[-1] == 29960
    assert max(b - a for a, b in zip(times, times[1:])) <= 3800
    assert events.select_event_samples(points, base, 0, 30000, 12, value) == (chosen, metadata)


def test_local_transient_has_priority_over_whole_frame_flash():
    points, base = grid()
    value = scan([(stamp, 0.8 if stamp == 9000 else 0.04 if stamp == 1200 else 0.0,
                   stamp == 9000) for stamp in range(0, 30000, 200)])
    _, metadata = events.select_event_samples(points, base, 0, 30000, 12, value)
    assert metadata['event_center_ms'] == 1200
    assert metadata['event_global_change'] is False
    assert metadata['semantic_actions_detected'] is False


def test_cut_is_context_candidate_when_no_local_transient_exists():
    points, base = grid()
    value = scan([(stamp, 0.9 if stamp == 9200 else 0.0, True) for stamp in range(0, 30000, 200)])
    chosen, metadata = events.select_event_samples(points, base, 0, 30000, 12, value)
    assert {9000, 9200, 9400} <= {pts for _, pts in chosen}
    assert metadata['event_global_change'] is True


def test_vfr_sparse_frames_and_window_boundaries_are_never_invented():
    points = [0, 11, 80, 120, 400, 401, 950, 1100, 1500, 1530, 1700, 1999, 2000]
    value = scan([(stamp, 0.3 if stamp == 1000 else 0.0, False) for stamp in range(0, 2200, 200)])
    chosen, _ = events.select_event_samples(points, Fraction(1, 1000), 50, 2000, 12, value)
    actual = [pts for _, pts in chosen]
    assert actual == sorted(set(actual))
    assert len(actual) <= 12 and set(actual) <= set(points)
    assert all(50 <= stamp < 2000 for stamp in actual)
    assert actual[0] == 80 and actual[-1] == 1999


def test_sparse_vfr_uses_distinct_points_when_uniform_targets_collide():
    points = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 29999]
    chosen, _ = events.select_event_samples(points, Fraction(1, 1000), 0, 30000, 12, scan([]))
    assert [pts for _, pts in chosen] == points


@pytest.mark.parametrize('count', [1, 2, 3, 4, 5])
def test_small_image_allowance_keeps_uniform_coverage(count):
    points, base = grid()
    chosen, metadata = events.select_event_samples(points, base, 0, 30000, count,
        scan([(1200, 1.0, False)]))
    assert len(chosen) == count and metadata['policy'] == 'uniform_no_transient'


@pytest.mark.parametrize('kwargs', [{'count': 13}, {'count': True}, {'start_ms': -1}, {'end_ms': 60001}])
def test_invalid_selection_parameters_fail_closed(kwargs):
    arguments = dict(start_ms=0, end_ms=30000, count=12)
    arguments.update(kwargs)
    with pytest.raises(ProcessingFailure):
        events.select_event_samples(*grid(), **arguments, scan=scan([]))


def test_real_scan_finds_short_local_visibility_that_uniform_grid_misses(settings, tmp_path):
    settings.FFMPEG_PATH = installed_media_binary('ffmpeg')
    path = tmp_path / 'short-visible-pulse.mp4'
    subprocess.run([settings.FFMPEG_PATH, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=gray:size=320x180:rate=10', '-t', '30', '-vf',
        "drawbox=x=80:y=60:w=60:h=40:color=white:t=fill:enable='between(t,1.1,1.7)'",
        '-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p', str(path)],
        check=True, timeout=20)
    context = LocalContext()
    result = events.scan_events(path, context, duration_ms=30000, deadline=time.monotonic() + 30)
    points, base = grid()
    chosen, metadata = events.select_event_samples(points, base, 0, 30000, 12, result)
    uniform, _ = events.select_event_samples(points, base, 0, 30000, 12, scan([]))
    assert any(1100 <= pts <= 1700 for _, pts in chosen)
    assert not any(1100 <= pts <= 1700 for _, pts in uniform)
    assert len(chosen) == len(uniform) == 12
    assert result.metrics['scan_frames'] == 150
    assert result.metrics['scan_bytes'] == 150 * events.SCAN_FRAME_BYTES
    assert len(context.calls) == 1
    assert not list(settings.PRIVATE_MEDIA_ROOT.glob('temporary-scans/*'))
    assert metadata['event_targets_ms']


def test_cancelled_scan_cleans_only_its_temporary_file(settings, tmp_path):
    settings.FFMPEG_PATH = installed_media_binary('ffmpeg')
    path = tmp_path / 'untouched.mp4'
    path.write_bytes(b'untouched')
    def cancel(argv, timeout):
        from pathlib import Path
        Path(argv[-1]).write_bytes(b'partial')
        raise Cancelled()
    context = SimpleNamespace(check=lambda: None, run=cancel)
    with pytest.raises(Cancelled):
        events.scan_events(path, context, duration_ms=30000, deadline=time.monotonic() + 30)
    assert path.read_bytes() == b'untouched'
    assert not list(settings.PRIVATE_MEDIA_ROOT.glob('temporary-scans/*'))


def test_expired_deadline_does_not_launch_scan_and_cleans_directory(settings, tmp_path):
    settings.FFMPEG_PATH = installed_media_binary('ffmpeg')
    path = tmp_path / 'untouched.mp4'
    path.write_bytes(b'untouched')
    context = LocalContext()
    with pytest.raises(ProcessingFailure) as error:
        events.scan_events(path, context, duration_ms=30000, deadline=time.monotonic() - 1)
    assert error.value.code == 'FRAME_EXTRACTION_TIMEOUT'
    assert context.calls == []
    assert not list(settings.PRIVATE_MEDIA_ROOT.glob('temporary-scans/*'))


def test_truncated_scan_is_rejected_and_cleaned(settings, tmp_path):
    settings.FFMPEG_PATH = installed_media_binary('ffmpeg')
    path = tmp_path / 'untouched.mp4'
    path.write_bytes(b'untouched')
    def truncated(argv, timeout):
        from pathlib import Path
        Path(argv[-1]).write_bytes(b'\x00' * (events.SCAN_FRAME_BYTES + 1))
    context = SimpleNamespace(check=lambda: None, run=truncated)
    with pytest.raises(ProcessingFailure) as error:
        events.scan_events(path, context, duration_ms=30000, deadline=time.monotonic() + 30)
    assert error.value.code == 'EVENT_SCAN_INVALID'
    assert not list(settings.PRIVATE_MEDIA_ROOT.glob('temporary-scans/*'))


@pytest.mark.parametrize('duration', [0, -1, True, 600001])
def test_scan_duration_limit_prevents_subprocess(duration):
    context = LocalContext()
    with pytest.raises(ProcessingFailure):
        events.scan_events('irrelevant', context, duration_ms=duration, deadline=time.monotonic() + 30)
    assert context.calls == []


def test_short_policy_distributes_two_motion_pairs_without_increasing_image_count():
    points, base = grid()
    signal = scan([(stamp, {1200: .1, 5200: .08, 21200: .05}.get(stamp, 0), False)
                   for stamp in range(0, 30000, 200)])
    chosen, metadata = events.select_short_event_samples(points, base, 0, 30000, 12, signal)
    times = [pts for _, pts in chosen]
    assert len(times) == len(set(times)) == 12
    assert times == sorted(times) and times[0] == 0 and times[-1] == 29960
    # A third temporal region wins over a second peak in the first region.
    assert {1000, 1400, 21000, 21400} <= set(times)
    assert metadata['anchor_count'] == 8
    assert max(b - a for a, b in zip(times, times[1:])) <= 4320
    assert [group['peak_ms'] for group in metadata['event_groups']] == [1200, 21200]
    assert metadata['semantic_actions_detected'] is False
    assert metadata['series_kind'] == 'short_pairs'
    assert events.select_short_event_samples(points, base, 0, 30000, 12, signal) == (chosen, metadata)


def test_short_pairs_keep_context_on_both_sides_without_claiming_three_phases():
    points, base = grid()
    signal = scan([(stamp, {800: .03, 1000: .04, 1200: .1, 1400: .03}.get(stamp, 0), False)
                   for stamp in range(0, 30000, 200)])
    chosen, metadata = events.select_short_event_samples(points, base, 0, 30000, 12, signal)
    assert {1000, 1400} <= {pts for _, pts in chosen}
    assert len(metadata['event_groups']) == 1
    assert metadata['anchor_count'] == 10
    assert metadata['event_groups'][0]['target_times_ms'] == [1000, 1400]


@pytest.mark.parametrize('score,broad', [(0, False), (.02, False), (.8, True)])
def test_short_policy_keeps_full_grid_for_static_steady_and_global_changes(score, broad):
    points, base = grid()
    signal = scan([(stamp, score, broad) for stamp in range(0, 30000, 200)])
    chosen, metadata = events.select_short_event_samples(points, base, 0, 30000, 12, signal)
    assert len(chosen) == 12 and metadata['anchor_count'] == 12
    assert metadata['policy'] == 'uniform_no_short_transient'
    assert metadata['event_groups'] == []
    assert max(b[1] - a[1] for a, b in zip(chosen, chosen[1:])) <= 2760


def test_short_series_does_not_cross_a_broad_cut_or_let_it_dominate_local_motion():
    points, base = grid()
    signal = scan([(stamp, {1000: .03, 1200: .8, 1400: .04, 21000: .02}.get(stamp, 0), stamp == 1200)
                   for stamp in range(0, 30000, 200)])
    _, metadata = events.select_short_event_samples(points, base, 0, 30000, 12, signal)
    # A series entirely after the cut is allowed; the cut itself and a
    # pre-cut series whose final sample crosses it are not motion candidates.
    assert [group['peak_ms'] for group in metadata['event_groups']] == [1400, 21000]
    assert all(not group['start_ms'] < 1200 <= group['end_ms'] for group in metadata['event_groups'])


def test_short_policy_rejects_collapsed_vfr_series_and_preserves_actual_source_points():
    points = [80, 120, 400, 401, 950, 1100, 1500, 1530, 1700, 1999, 4000, 10000]
    signal = scan([(stamp, .1 if stamp == 8000 else 0, False) for stamp in range(0, 10200, 200)])
    chosen, metadata = events.select_short_event_samples(points, Fraction(1, 1000), 50, 10001, 12, signal)
    assert [pts for _, pts in chosen] == points
    assert metadata['event_groups'] == []
    assert metadata['anchor_count'] <= 12


@pytest.mark.parametrize('count', range(1, 13))
def test_short_policy_respects_each_frame_allowance_and_bounds(count):
    points, base = grid()
    signal = scan([(stamp, .1 if stamp in (600, 21200) else 0, False)
                   for stamp in range(0, 30000, 200)])
    chosen, metadata = events.select_short_event_samples(points, base, 400, 29900, count, signal)
    assert len(chosen) == count
    assert len({pts for _, pts in chosen}) == count
    assert all(400 <= pts < 29900 for _, pts in chosen)
    for group in metadata['event_groups']:
        assert len(set(group['selected_timestamps_ms'])) == 2
        assert set(group['selected_timestamps_ms']) <= {pts for _, pts in chosen}
    if count < 9:
        assert metadata['event_groups'] == []


@pytest.mark.parametrize('kwargs', [{'count': 13}, {'count': True}, {'start_ms': -1}, {'end_ms': 60001}])
def test_invalid_short_selection_parameters_fail_closed(kwargs):
    arguments = dict(start_ms=0, end_ms=30000, count=12)
    arguments.update(kwargs)
    with pytest.raises(ProcessingFailure):
        events.select_short_event_samples(*grid(), **arguments, scan=scan([]))


def test_real_scan_captures_two_short_visibility_events_with_one_local_pass(settings, tmp_path):
    settings.FFMPEG_PATH = installed_media_binary('ffmpeg')
    path = tmp_path / 'two-short-visible-pulses.mp4'
    subprocess.run([settings.FFMPEG_PATH, '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=gray:size=320x180:rate=10', '-t', '30', '-vf',
        "drawbox=x=80:y=60:w=60:h=40:color=white:t=fill:enable='between(t,1.1,1.7)+between(t,21.1,21.7)'",
        '-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p', str(path)],
        check=True, timeout=20)
    context = LocalContext()
    result = events.scan_events(path, context, duration_ms=30000, deadline=time.monotonic() + 30)
    points, base = grid()
    chosen, metadata = events.select_short_event_samples(points, base, 0, 30000, 12, result)
    previous, _ = events.select_event_samples(points, base, 0, 30000, 12, result)
    assert any(1100 <= pts <= 1700 for _, pts in chosen)
    assert any(21100 <= pts <= 21700 for _, pts in chosen)
    assert not any(21100 <= pts <= 21700 for _, pts in previous)
    assert len(chosen) == len(previous) == 12
    assert len(metadata['event_groups']) == 2
    assert len(context.calls) == 1
    assert not list(settings.PRIVATE_MEDIA_ROOT.glob('temporary-scans/*'))
