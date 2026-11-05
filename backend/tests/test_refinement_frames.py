"""Refinements add samples without losing trusted parent images; no model calls."""
import hashlib
from fractions import Fraction

import pytest

from media_pipeline import frames
from media_pipeline.exceptions import ProcessingFailure
from .test_video_frames import video_asset


def test_equivalent_clocks_resolve_exact_native_points_and_deduplicate():
    required = frames._required_points(
        [{'pts': 1, 'time_base': '1/25'}, {'pts': 40, 'time_base': '1/1000'},
         {'pts': 3, 'time_base': '1/25'}],
        [0, 512, 1024, 1536], Fraction(1, 12800), 0, 160, 2,
    )
    assert required == [512, 1536]


@pytest.mark.parametrize('required', [
    None, {}, 'frames', [None], [{}], [{'pts': 1}],
    [{'pts': True, 'time_base': '1/1000'}],
    [{'pts': 80.0, 'time_base': '1/1000'}],
    [{'pts': -1, 'time_base': '1/1000'}],
    [{'pts': 2 ** 64, 'time_base': '1/1000'}],
    [{'pts': 80, 'time_base': 0.001}],
    [{'pts': 80, 'time_base': '0/1000'}],
    [{'pts': 80, 'time_base': '1/0'}],
    [{'pts': 80, 'time_base': '-1/1000'}],
    [{'pts': 80, 'time_base': '2'}],
    [{'pts': 80, 'time_base': '1/' + '1' * 100}],
    [{'pts': 1, 'time_base': '1/3000'}],  # Fractional native PTS is never rounded.
    [{'pts': 90, 'time_base': '1/1000'}],  # No such source packet.
    [{'pts': 0, 'time_base': '1/1000'}],   # Before this child's interval.
    [{'pts': 200, 'time_base': '1/1000'}], # Child's end is exclusive.
    [{'pts': 80, 'time_base': '1/1000'}, {'pts': 120, 'time_base': '1/1000'}],
])
def test_bad_foreign_or_excessive_required_points_fail_closed(required):
    with pytest.raises(ProcessingFailure) as error:
        frames._required_points(required, [0, 80, 120, 200], Fraction(1, 1000), 40, 200, 1)
    assert error.value.code == 'FRAME_REFINEMENT_INVALID'


def test_largest_gap_fill_preserves_irregular_action_anchors_and_fills_allowance():
    points = [0, 40, 80, 160, 320, 360, 400, 480, 520, 560, 720, 800, 840, 880, 960]
    required = [80, 520, 560]
    selected = frames._select_anchored_samples(points, Fraction(1, 1000), 0, 1000, 8, required)
    actual = [pts for _, pts in selected]
    assert len(actual) == len(set(actual)) == 8
    assert actual == sorted(actual) and set(required) <= set(actual) <= set(points)
    assert max(b - a for a, b in zip([0, *actual], [*actual, 1000])) <= 240
    assert selected == frames._select_anchored_samples(points, Fraction(1, 1000), 0, 1000, 8, list(reversed(required)))


def test_sparse_vfr_uses_every_available_packet_without_duplicates_or_rounding():
    points = [0, 80, 120, 400, 401, 950]
    selected = frames._select_anchored_samples(points, Fraction(1, 1000), 0, 1000, 12, [400, 401])
    assert selected == [(pts, pts) for pts in points]


def test_full_anchor_allowance_keeps_exact_parent_points():
    points = list(range(0, 1000, 40))
    required = [0, 40, 80, 200, 400, 960]
    selected = frames._select_anchored_samples(points, Fraction(1, 1000), 0, 1000, 6, required)
    assert [pts for _, pts in selected] == required


@pytest.mark.django_db
def test_children_keep_actual_parent_jpegs_and_private_pts_within_same_cap(video_asset, monkeypatch):
    asset, context = video_asset
    with frames.extract_video_frames(asset, context, count=12, strategy='uniform') as parent:
        manifest = parent.manifest()
        original = {(frame.pts * Fraction(frame.time_base)): frame.sha256 for frame in parent.frames}
    # An anchored refinement needs no extra visual-event scan.
    from media_pipeline import event_sampling
    monkeypatch.setattr(event_sampling, 'scan_events', lambda *args, **kwargs: pytest.fail('unexpected event scan'))
    children = {}
    for start, end in ((0, 1000), (1000, 2000)):
        required = [{'pts': frame['pts'], 'time_base': frame['time_base']} for frame in manifest['frames']
                    if start <= frame['pts'] * Fraction(frame['time_base']) * 1000 < end]
        with frames.extract_video_frames(asset, context, start_ms=start, end_ms=end,
                                         count=12, strategy='short_events', required_frames=required) as child:
            assert len(child.frames) == 12
            assert child.selection['policy'] == 'anchored_refinement'
            assert child.sampling_version == 'anchored-refinement-decoded-pts-v2'
            assert child.selection['anchor_count'] == len(required)
            assert child.selection['boundary_policy'] == 'spare_slots_farthest_endpoint_first'
            assert all(point['selected'] for point in child.selection['boundary_samples'])
            assert child.processing['ffmpeg_processes'] == 1
            for frame in child.frames:
                assert frame.path.stat().st_mode & 0o777 == 0o600
                assert hashlib.sha256(frame.path.read_bytes()).hexdigest() == frame.sha256
                assert start <= frame.pts * Fraction(frame.time_base) * 1000 < end
                children[frame.pts * Fraction(frame.time_base)] = frame.sha256
            directory = child.directory
        assert not directory.exists()
    assert all(children[stamp] == digest for stamp, digest in original.items())


@pytest.mark.django_db
def test_invalid_required_point_never_reaches_decoder(video_asset, monkeypatch):
    asset, context = video_asset
    monkeypatch.setattr(frames, '_decode_frames', lambda *args, **kwargs: pytest.fail('unexpected decode'))
    with pytest.raises(ProcessingFailure) as error:
        frames.extract_video_frames(asset, context, required_frames=[{'pts': 1, 'time_base': '1/100000000'}])
    assert error.value.code == 'FRAME_REFINEMENT_INVALID'


@pytest.mark.django_db
def test_explicit_empty_anchors_keep_default_selection_and_image_hashes(video_asset):
    asset, context = video_asset
    with frames.extract_video_frames(asset, context, count=8, strategy='short_events') as first:
        expected = [(frame.pts, frame.time_base, frame.sha256) for frame in first.frames]
        selection, version = first.selection, first.sampling_version
    with frames.extract_video_frames(asset, context, count=8, strategy='short_events', required_frames=[]) as empty:
        assert [(frame.pts, frame.time_base, frame.sha256) for frame in empty.frames] == expected
        assert empty.selection == selection and empty.sampling_version == version
