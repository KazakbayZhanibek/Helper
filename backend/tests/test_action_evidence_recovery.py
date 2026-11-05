"""Conservative upload recovery; these fixtures do not prove visual accuracy."""
from copy import deepcopy
import uuid

import pytest

from ai_drafts.validation import InvalidModelOutput
from visual_drafts import action_evidence
from .test_action_evidence import frames, session, ids, payload, observation, initial_state, action_only


def partial_payload(frames):
    value = payload(frames)
    second = observation(frames, item_id='o2')
    unsupported = observation(frames, item_id='o3')
    unsupported['action_frame_ids'] = ids(frames, 1, 2)
    value['observations'].extend([unsupported, second])
    return value


def test_upload_keeps_two_supported_actions_and_discards_one_claim_into_review(frames, session):
    value = partial_payload(frames)
    snapshot = deepcopy(value)
    result = action_evidence.normalize(value, frames, session)
    assert value == snapshot
    assert len(result['steps']) == 2
    assert [item['id'].split(':')[-1] for item in result['observations']] == ['o1', 'o2']
    assert all(item['description'] == result['steps'][0]['description'] for item in result['steps'])
    assert len(result['gaps']) == 1
    gap = result['gaps'][0]
    assert gap['reason'] == 'unclear_action'
    assert gap['description'] == action_evidence.PHASE_REVIEW_REASON
    assert gap['evidence_frame_ids'] == ids(frames, 0, 4)
    assert (gap['start_ms'], gap['end_ms']) == (0, 4000)
    assert gap['id'].endswith(':r1')
    assert 'uncertain_action' in {item['code'] for item in result['warnings']}
    assert all(not link.endswith(':o3') for step in result['steps'] for link in step['observation_ids'])


@pytest.mark.parametrize('change', [
    {'before_frame_id': None}, {'movement': None}, {'action_frame_ids': []},
    {'before_frame_id': str(uuid.UUID(int=5))},
])
def test_upload_local_phase_failure_is_a_question_without_repair(frames, session, change):
    value = payload(frames)
    value['observations'][0].update(change)
    # A reversed before/after timeline is deliberately a hard ordering failure.
    if change.get('before_frame_id') == str(uuid.UUID(int=5)):
        value['observations'][0]['after'] = value['observations'][0]['after_frame_id'] = None
    result = action_evidence.normalize(value, frames, session)
    assert result['steps'] == result['observations'] == []
    assert len(result['gaps']) == 1
    assert result['gaps'][0]['description'] == action_evidence.PHASE_REVIEW_REASON
    assert 'insufficient_evidence' in {item['code'] for item in result['warnings']}


@pytest.mark.parametrize('field', ['evidence_frame_ids', 'action_frame_ids', 'before_frame_id', 'after_frame_id'])
def test_foreign_reference_cannot_hide_behind_an_earlier_recoverable_phase_error(frames, session, field):
    value = partial_payload(frames)
    bad = value['observations'][1]
    bad['before'] = None  # Phase error occurs before old action/after validation.
    foreign = str(uuid.UUID(int=999))
    bad[field] = [foreign] if field == 'action_frame_ids' else (
        [str(frames[0].id), foreign] if field == 'evidence_frame_ids' else foreign)
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_UNKNOWN'):
        action_evidence.normalize(value, frames, session)


@pytest.mark.parametrize('field', ['evidence_frame_ids', 'action_frame_ids'])
@pytest.mark.parametrize('kind', ['duplicate', 'reverse', 'skip'])
def test_reference_order_and_duplicates_remain_fatal_despite_phase_error(frames, session, field, kind):
    value = partial_payload(frames)
    bad = value['observations'][1]
    bad['before'] = None
    bad[field] = {'duplicate': ids(frames, 0, 1) * 2,
                  'reverse': ids(frames, 0, 3)[::-1], 'skip': ids(frames, 0, 3)[::2]}[kind]
    with pytest.raises(InvalidModelOutput, match='VISUAL_(OUTPUT|EVIDENCE_ORDER)_INVALID'):
        action_evidence.normalize(value, frames, session)


@pytest.mark.parametrize('mutation', ['duplicate_id', 'extra_field', 'blank_after', 'phase_order'])
def test_other_invalid_observation_fields_are_never_salvaged(frames, session, mutation):
    value = partial_payload(frames)
    bad = value['observations'][1]
    if mutation == 'duplicate_id':
        bad['id'] = 'o1'
    elif mutation == 'extra_field':
        bad['start_ms'] = 0
    elif mutation == 'blank_after':
        bad['after'] = '  '
    else:
        bad.update(before_frame_id=str(frames[3].id), after_frame_id=str(frames[0].id))
    with pytest.raises(InvalidModelOutput):
        action_evidence.normalize(value, frames, session)


def test_live_still_rejects_an_unsupported_action_among_valid_actions(frames, session):
    session.mode = 'live'
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_PHASE_INVALID'):
        action_evidence.normalize(partial_payload(frames), frames, session)


def changed_state(frames):
    item = initial_state(frames)
    item.update(before='Предмета на поверхности не видно.', after='Предмет лежит на поверхности.',
                after_frame_id=str(frames[1].id))
    return item


def test_unexplained_state_change_preserves_context_and_asks_without_inventing_action(frames, session):
    value = payload(frames)
    state = changed_state(frames)
    action = action_only(frames, 1, 4)
    action['id'] = 'o2'
    value['observations'] = [state, action]
    result = action_evidence.normalize(value, frames, session)
    assert len(result['steps']) == 1
    assert result['observations'][0]['kind'] == 'state'
    assert result['observations'][0]['movement'] is None
    assert result['observations'][0]['before'] == state['before']
    assert result['observations'][0]['after'] == state['after']
    gap = result['gaps'][0]
    assert gap['description'] == action_evidence.STATE_CHANGE_REVIEW_REASON
    assert gap['evidence_frame_ids'] == ids(frames, 0, 2)
    assert (gap['start_ms'], gap['end_ms']) == (0, 2000)


@pytest.mark.parametrize('case', ['same_text', 'same_frame', 'missing_before', 'missing_after', 'covered', 'live'])
def test_state_change_question_only_appears_for_an_uncovered_change(frames, session, case):
    value = payload(frames)
    state = changed_state(frames)
    value['observations'] = [state]
    if case == 'same_text':
        state['after'] = '  ПРЕДМЕТА\n на поверхности не видно.  '
    elif case == 'same_frame':
        state['after_frame_id'] = state['before_frame_id']
    elif case.startswith('missing_'):
        phase = case.split('_')[1]
        state[phase] = state[f'{phase}_frame_id'] = None
    elif case == 'covered':
        action = action_only(frames, 0, 3)
        action['id'] = 'o2'
        value['observations'].append(action)
    else:
        session.mode = 'live'
    result = action_evidence.normalize(value, frames, session)
    assert result['gaps'] == []


def test_review_ids_are_unique_and_deterministic_beside_model_gaps(frames, session):
    value = partial_payload(frames)
    state = changed_state(frames)
    state['id'] = 'o4'
    value['observations'].append(state)
    value['gaps'] = [{'reason': 'occluded', 'description': 'Предмет закрыт рукой.', 'evidence_frame_ids': ids(frames, 4, 6)}]
    result = action_evidence.normalize(value, frames, session)
    assert [item['id'].split(':')[-1] for item in result['gaps']] == ['g1', 'r1', 'r2']
    assert len({item['id'] for item in result['gaps']}) == 3
    assert action_evidence.normalize(value, frames, session) == result
    result['gaps'][1]['evidence_frame_ids'].clear()
    assert value['observations'][1]['evidence_frame_ids'] == ids(frames, 0, 4)
