from copy import deepcopy

import pytest

from visual_drafts import demonstrations as demo
from .test_demonstrations import answer, normalized, selected


@pytest.mark.parametrize('action_start,action_end,cut_start,cut_end', [
    (1, 3, 2, 4),  # The action enters a wider cut interval.
    (2, 4, 1, 3),  # The action leaves a wider cut interval.
    (2, 3, 1, 4),  # The cut interval encloses the action.
])
def test_partially_overlapping_cut_only_removes_affected_action(action_start, action_end, cut_start, cut_end):
    value = answer(5)
    action = value['actions'][0]
    action['moments'][0]['image_index'] = action_start
    action['moments'][-1]['image_index'] = action_end
    value['gaps'] = [{'from_image_index': cut_start, 'to_image_index': cut_end,
                      'reason': 'cut', 'question': 'Что показано после склейки?'}]
    content, source = normalized(value, 5)
    assert not content['steps']
    assert any(gap['reason'] == 'unclear_action' for gap in content['gaps'])
    assert demo.reconcile_grounding(content, source, selected()) == content


def test_cut_touching_action_boundary_keeps_action_and_saved_projection():
    value = answer(3)
    value['actions'][0]['moments'][-1]['image_index'] = 2
    value['gaps'] = [{'from_image_index': 2, 'to_image_index': 3,
                      'reason': 'cut', 'question': 'Что показано после склейки?'}]
    content, source = normalized(value, 3)
    assert len(content['steps']) == 1
    assert demo.reconcile_grounding(content, source, selected()) == content


def test_partially_overlapping_cut_preserves_clear_neighbor():
    value = answer(5)
    neighbor = deepcopy(value['actions'][0])
    neighbor['moments'][-1]['image_index'] = 2
    neighbor['title'] = 'Берём заготовку'
    value['actions'][0]['moments'][0]['image_index'] = 2
    value['actions'][0]['moments'][-1]['image_index'] = 4
    value['actions'].insert(0, neighbor)
    value['gaps'] = [{'from_image_index': 3, 'to_image_index': 5,
                      'reason': 'cut', 'question': 'Что показано после склейки?'}]
    content, source = normalized(value, 5)
    assert [step['title'] for step in content['steps']] == ['Берём заготовку']
    assert demo.reconcile_grounding(content, source, selected()) == content
