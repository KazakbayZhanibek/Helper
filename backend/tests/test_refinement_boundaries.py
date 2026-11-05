"""Boundary context is reallocated inside the existing frame allowance."""
from fractions import Fraction

import pytest

from media_pipeline.frames import _select_anchored_samples


def choose(points, required, count, start=0, end=1000):
    return [pts for _, pts in _select_anchored_samples(
        points, Fraction(1, 1000), start, end, count, required)]


def test_two_spare_slots_cover_both_actual_endpoints_and_keep_all_parent_anchors():
    points = list(range(0, 1000, 40))
    required = [200, 400, 520, 720]
    assert choose(points, required, 6) == [0, 200, 400, 520, 720, 960]
    assert required == [200, 400, 520, 720]


@pytest.mark.parametrize(('required', 'endpoint'), [
    ([120, 400], 960),             # Longer uncovered right boundary.
    ([520, 840], 0),              # Longer uncovered left boundary.
    ([160, 800], 0),              # Equal native-PTS distances: earlier wins.
])
def test_one_spare_slot_prefers_largest_uncovered_boundary_with_stable_tie(required, endpoint):
    points = list(range(0, 1000, 40))
    expected = sorted([*required, endpoint])
    assert choose(points, required, 3) == expected
    assert choose(points, list(reversed(required)), 3) == expected


def test_full_required_allowance_never_sacrifices_an_anchor_for_an_endpoint():
    required = [40, 200, 400, 560, 800]
    assert choose(list(range(0, 1000, 40)), required, len(required)) == required


def test_existing_endpoints_leave_the_spare_slots_for_interior_gaps():
    assert choose(list(range(0, 1000, 40)), [0, 960], 5) == [0, 240, 480, 720, 960]


def test_sparse_vfr_and_exclusive_end_use_real_endpoint_packets():
    points = [0, 79, 80, 400, 401, 950, 999, 1000]
    actual = choose(points, [400, 401], 4, start=80, end=999)
    assert actual == [80, 400, 401, 950]


def test_distinct_adjacent_pts_are_not_assumed_to_be_duplicate_images():
    assert choose([0, 1, 500, 998, 999], [1, 500, 998], 5) == [0, 1, 500, 998, 999]


def test_single_available_pts_is_not_added_twice_as_two_endpoints():
    assert choose([500], [500], 12) == [500]


@pytest.mark.parametrize('count', range(1, 13))
def test_every_allowance_stays_bounded_ordered_and_retains_exact_required_points(count):
    points = list(range(0, 1000, 40))
    actual = choose(points, [400], count)
    assert len(actual) == len(set(actual)) == count
    assert actual == sorted(actual)
    assert 400 in actual and set(actual) <= set(points)
    if count >= 3:
        assert actual[0] == 0 and actual[-1] == 960


def test_chopping_windows_reallocate_two_interior_images_without_losing_parent_anchors():
    points = list(range(0, 11760, 40))
    first_required = [0, 1680, 3360, 5040]
    second_required = [6400, 6720, 6800, 8400, 8800, 9200, 10080, 11720]
    first = choose(points, first_required, 12, 0, 5880)
    second = choose(points, second_required, 12, 5880, 11760)
    assert set(first_required) <= set(first)
    assert set(second_required) <= set(second)
    assert len(first) == len(second) == 12
    assert first[-1] == 5840 and second[0] == 5880
    # These are still separate requests, not a newly verified motion pair.
    assert not set(first) & set(second)
