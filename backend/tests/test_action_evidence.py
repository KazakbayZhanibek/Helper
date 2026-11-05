"""Local contract checks; fixtures do not establish recognition accuracy."""
import copy
import json
import uuid
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from ai_drafts.validation import InvalidModelOutput
from visual_drafts import action_evidence


@pytest.fixture
def frames():
    return [SimpleNamespace(id=uuid.UUID(int=index + 1), coverage_start_ms=index * 1000,
                            coverage_end_ms=(index + 1) * 1000) for index in range(6)]


@pytest.fixture
def session():
    return SimpleNamespace(language='ru', mode='upload', asset=None, window_start_ms=0, window_end_ms=6000)


def ids(frames, start=0, end=None):
    return [str(frame.id) for frame in frames[start:end]]


def observation(frames, *, item_id='o1'):
    return {
        'id': item_id, 'kind': 'action', 'subject': 'Рука и предмет',
        'title': 'Переместите предмет',
        'before': 'Предмет слева от руки.', 'before_frame_id': str(frames[0].id),
        'movement': 'Рука удерживает предмет и перемещает его вправо.',
        'action_frame_ids': ids(frames, 1, 3),
        'after': 'Предмет справа от исходного положения.', 'after_frame_id': str(frames[3].id),
        'evidence_frame_ids': ids(frames, 0, 4),
    }


def payload(frames):
    return {'schema_version': '3', 'language': 'ru', 'observations': [observation(frames)], 'gaps': [], 'warnings': []}


def initial_state(frames):
    value = observation(frames)
    value.update(kind='state', title='Исходное положение предмета',
                 before='Предмет находится на поверхности.', movement=None, action_frame_ids=[],
                 after=None, after_frame_id=None, evidence_frame_ids=ids(frames, 0, 2))
    return value


def action_only(frames, start, end):
    value = observation(frames)
    value.update(before=None, before_frame_id=None, after=None, after_frame_id=None,
                 action_frame_ids=ids(frames, start, end), evidence_frame_ids=ids(frames, start, end))
    return value


def test_valid_action_derives_steps_from_movement_and_server_frames(frames, session):
    Draft202012Validator.check_schema(action_evidence.SCHEMA)
    value = payload(frames)
    result = action_evidence.normalize(value, frames, session)
    observed = result['observations'][0]
    assert observed['id'].endswith(':o1') and observed['id'] != 'o1'
    assert (observed['start_ms'], observed['end_ms']) == (0, 4000)
    assert result['schema_version'] == '3'
    assert result['step_format'] == 'teaching_v1'
    assert result['steps'] == [{
        'title': value['observations'][0]['title'],
        'description': 'С чем работаем\nРука и предмет\n\nПеред началом на видео\nПредмет слева от руки.\n\nКак выполнить\nРука удерживает предмет и перемещает его вправо.\n\nЧто сравнить с видео после действия\nПредмет справа от исходного положения.',
        'evidence_frame_ids': ids(frames, 1, 3),
        'observation_ids': [observed['id']],
        'start_ms': 1000, 'end_ms': 3000,
    }]
    assert {warning['code'] for warning in result['warnings']} == {'sampled_frames', 'approximate_timing'}


def test_initial_state_with_only_before_is_retained_without_an_invented_action(frames, session):
    value = payload(frames)
    value['observations'] = [initial_state(frames)]
    result = action_evidence.normalize(value, frames, session)
    assert result['steps'] == []
    assert result['observations'][0]['before'] == value['observations'][0]['before']
    assert result['observations'][0]['after'] is None
    assert result['observations'][0]['movement'] is None
    assert (result['observations'][0]['start_ms'], result['observations'][0]['end_ms']) == (0, 2000)
    assert 'insufficient_evidence' in {warning['code'] for warning in result['warnings']}


@pytest.mark.parametrize('missing', [('before',), ('after',), ('before', 'after')])
def test_partial_action_does_not_require_unseen_phases(frames, session, missing):
    value = payload(frames)
    item = value['observations'][0]
    for phase in missing:
        item[phase] = item[f'{phase}_frame_id'] = None
    item['evidence_frame_ids'] = ids(frames, 1 if 'before' in missing else 0, 3 if 'after' in missing else 4)
    result = action_evidence.normalize(value, frames, session)
    assert len(result['steps']) == 1
    for phase in missing:
        assert result['observations'][0][phase] is None


def test_state_with_only_after_is_allowed(frames, session):
    value = payload(frames)
    item = initial_state(frames)
    item.update(after=item['before'], after_frame_id=str(frames[1].id), before=None, before_frame_id=None)
    value['observations'] = [item]
    assert action_evidence.normalize(value, frames, session)['steps'] == []


@pytest.mark.parametrize('field', ['evidence_frame_ids', 'action_frame_ids', 'before_frame_id', 'after_frame_id'])
def test_unknown_or_foreign_frame_is_not_accepted_by_uuid_shape(frames, session, field):
    value = payload(frames)
    # A valid UUID from outside this supplied batch conveys no evidence ownership.
    foreign_id = str(uuid.UUID(int=999))
    if field.endswith('_ids'):
        value['observations'][0][field][-1] = foreign_id
    else:
        value['observations'][0][field] = foreign_id
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_UNKNOWN'):
        action_evidence.normalize(value, frames, session)


@pytest.mark.parametrize('target', ['observation', 'action', 'gap'])
@pytest.mark.parametrize('bad_range', ['reordered', 'sparse', 'duplicate'])
def test_evidence_ranges_must_be_contiguous_unique_and_in_batch_order(frames, session, target, bad_range):
    value = payload(frames)
    refs = {'reordered': ids(frames, 0, 3)[::-1], 'sparse': ids(frames, 0, 3)[::2],
            'duplicate': [str(frames[0].id), str(frames[0].id)]}[bad_range]
    if target == 'gap':
        value['gaps'] = [{'reason': 'sampling_gap', 'description': 'Между кадрами есть пропуск.', 'evidence_frame_ids': refs}]
    else:
        value['observations'][0]['action_frame_ids' if target == 'action' else 'evidence_frame_ids'] = refs
    with pytest.raises(InvalidModelOutput):
        action_evidence.normalize(value, frames, session)


def test_gap_cannot_reference_a_frame_outside_the_batch(frames, session):
    value = payload(frames)
    value['gaps'] = [{'reason': 'sampling_gap', 'description': 'Пропуск.',
                      'evidence_frame_ids': [str(frames[0].id), str(uuid.UUID(int=999))]}]
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_UNKNOWN'):
        action_evidence.normalize(value, frames, session)


def test_duplicate_observation_ids_cannot_alias_two_claims(frames, session):
    value = payload(frames)
    value['observations'].append(copy.deepcopy(value['observations'][0]))
    with pytest.raises(InvalidModelOutput, match='VISUAL_OUTPUT_INVALID'):
        action_evidence.normalize(value, frames, session)


@pytest.mark.parametrize('change', [
    {'before_frame_id': None}, {'before': None}, {'after_frame_id': None}, {'after': None},
    {'before_frame_id': str(uuid.UUID(int=3))}, {'after_frame_id': str(uuid.UUID(int=2))},
    {'before_frame_id': str(uuid.UUID(int=5))}, {'movement': None}, {'action_frame_ids': []},
    {'action_frame_ids': [str(uuid.UUID(int=2))]},
    {'action_frame_ids': [str(uuid.UUID(int=4)), str(uuid.UUID(int=5))]},
])
def test_live_invalid_phase_pairs_order_and_action_support_are_rejected(frames, session, change):
    session.mode = 'live'
    value = payload(frames)
    value['observations'][0].update(change)
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_(PHASE|ORDER)_INVALID'):
        action_evidence.normalize(value, frames, session)


@pytest.mark.parametrize('start,end', [(0, 4), (1, 5)])
def test_live_action_evidence_cannot_be_padded_beyond_supported_phases(frames, session, start, end):
    session.mode = 'live'
    value = payload(frames)
    value['observations'] = [action_only(frames, 1, 3)]
    value['observations'][0]['evidence_frame_ids'] = ids(frames, start, end)
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_PHASE_INVALID'):
        action_evidence.normalize(value, frames, session)


@pytest.mark.parametrize('change', [
    {'movement': 'Рука перемещает предмет.'}, {'action_frame_ids': [str(uuid.UUID(int=1)), str(uuid.UUID(int=2))]},
    {'before': None, 'before_frame_id': None},
    {'before_frame_id': str(uuid.UUID(int=2)), 'after': 'Предмет виден.', 'after_frame_id': str(uuid.UUID(int=1))},
])
def test_live_state_requires_a_phase_and_cannot_hide_an_action(frames, session, change):
    session.mode = 'live'
    value = payload(frames)
    value['observations'] = [initial_state(frames)]
    value['observations'][0].update(change)
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_(PHASE|ORDER)_INVALID'):
        action_evidence.normalize(value, frames, session)


@pytest.mark.parametrize('action_range', [(1, 5), (2, 4)])
def test_action_spanning_both_sides_of_declared_cut_is_rejected(frames, session, action_range):
    value = payload(frames)
    value['observations'] = [action_only(frames, *action_range)]
    value['gaps'] = [{'reason': 'cut', 'description': 'Между кадрами меняется сцена.', 'evidence_frame_ids': ids(frames, 2, 4)}]
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_CUT_INVALID'):
        action_evidence.normalize(value, frames, session)


@pytest.mark.parametrize('action_range', [(1, 3), (3, 5)])
def test_touching_one_side_of_cut_does_not_reject_supported_action(frames, session, action_range):
    value = payload(frames)
    value['observations'] = [action_only(frames, *action_range)]
    value['gaps'] = [{'reason': 'cut', 'description': 'Между кадрами меняется сцена.', 'evidence_frame_ids': ids(frames, 2, 4)}]
    result = action_evidence.normalize(value, frames, session)
    assert len(result['steps']) == 1
    assert 'uncertain_action' in {warning['code'] for warning in result['warnings']}


def test_simultaneous_actions_are_not_rejected_as_invalid_overlap(frames, session):
    value = payload(frames)
    value['observations'].append(observation(frames, item_id='o2'))
    result = action_evidence.normalize(value, frames, session)
    assert len(result['steps']) == 2
    assert result['steps'][0]['observation_ids'] != result['steps'][1]['observation_ids']


@pytest.mark.parametrize('reason,code', [
    ('occluded', 'occluded_view'), ('sampling_gap', 'sampled_frames'), ('cut', 'uncertain_action'),
    ('outside_recording', 'partial_video'), ('unclear_action', 'uncertain_action'),
])
def test_gaps_survive_without_steps_and_have_server_times_and_existing_warning_codes(frames, session, reason, code):
    value = payload(frames)
    value['observations'] = []
    value['gaps'] = [{'reason': reason, 'description': 'Действие не видно полностью.', 'evidence_frame_ids': ids(frames, 2, 4)}]
    result = action_evidence.normalize(value, frames, session)
    assert result['steps'] == []
    assert len(result['gaps']) == 1
    assert result['gaps'][0]['id'].endswith(':g1')
    assert (result['gaps'][0]['start_ms'], result['gaps'][0]['end_ms']) == (2000, 4000)
    assert {code, 'insufficient_evidence'} <= {warning['code'] for warning in result['warnings']}


@pytest.mark.parametrize('field', ['subject', 'title', 'before', 'movement', 'after', 'gap', 'warning'])
def test_whitespace_cannot_stand_in_for_observation_or_warning_text(frames, session, field):
    value = payload(frames)
    if field == 'gap':
        value['gaps'] = [{'reason': 'occluded', 'description': '   ', 'evidence_frame_ids': ids(frames, 0, 2)}]
    elif field == 'warning':
        value['warnings'] = [{'code': 'missing_parameters', 'message': '   '}]
    else:
        value['observations'][0][field] = '   '
    with pytest.raises(InvalidModelOutput, match='VISUAL_OUTPUT_INVALID'):
        action_evidence.normalize(value, frames, session)


def test_schema_valid_utf8_payload_over_32_kib_is_rejected(frames, session):
    value = payload(frames)
    value['observations'] = [observation(frames, item_id=f'o{index}') for index in range(1, 13)]
    for item in value['observations']:
        item['movement'] = '🟦' * 800
    assert action_evidence.VALIDATOR.is_valid(value)
    assert len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > action_evidence.MAX_OUTPUT_BYTES
    with pytest.raises(InvalidModelOutput, match='VISUAL_OUTPUT_INVALID'):
        action_evidence.normalize(value, frames, session)


@pytest.mark.parametrize('language', ['ru', 'kk', 'mixed'])
def test_language_is_bound_to_session(frames, session, language):
    value = payload(frames)
    value['language'] = session.language = language
    assert action_evidence.normalize(value, frames, session)['language'] == language
    session.language = 'kk' if language != 'kk' else 'ru'
    with pytest.raises(InvalidModelOutput, match='VISUAL_LANGUAGE_MISMATCH'):
        action_evidence.normalize(value, frames, session)


def test_namespaces_are_deterministic_for_a_batch_and_distinct_across_windows(frames, session):
    value = payload(frames)
    first = action_evidence.normalize(value, frames, session)['observations'][0]['id']
    assert action_evidence.normalize(value, frames, session)['observations'][0]['id'] == first
    changed_text = copy.deepcopy(value)
    changed_text['observations'][0]['title'] = 'Изменённое название'
    assert action_evidence.normalize(changed_text, frames, session)['observations'][0]['id'] == first
    other_batch = frames[1:]
    second = action_evidence.normalize(payload(other_batch), other_batch, session)['observations'][0]['id']
    assert first != second


def test_normalization_does_not_mutate_or_alias_input(frames, session):
    value = payload(frames)
    value['gaps'] = [{'reason': 'occluded', 'description': 'Рука закрывает предмет.', 'evidence_frame_ids': ids(frames, 3, 5)}]
    original = copy.deepcopy(value)
    result = action_evidence.normalize(value, frames, session)
    assert value == original
    result['observations'][0]['action_frame_ids'].append(str(frames[5].id))
    result['gaps'][0]['description'] = 'Изменено.'
    result['warnings'].append({'code': 'missing_parameters', 'message': 'Изменено.'})
    assert value == original
    assert result['steps'][0]['evidence_frame_ids'] == ids(frames, 1, 3)


def test_normalization_failure_does_not_mutate_input(frames, session):
    value = payload(frames)
    value['gaps'] = [{'reason': 'cut', 'description': 'Смена сцены.', 'evidence_frame_ids': ids(frames, 1, 3)}]
    original = copy.deepcopy(value)
    with pytest.raises(InvalidModelOutput):
        action_evidence.normalize(value, frames, session)
    assert value == original


@pytest.mark.parametrize('mode', ['live', 'upload'])
def test_session_warnings_preserved_within_maximum_warning_count(frames, session, mode):
    value = payload(frames)
    value['warnings'] = [{'code': 'missing_parameters', 'message': f'Параметр {index} не определён.'} for index in range(30)]
    value['gaps'] = [{'reason': 'occluded', 'description': 'Есть закрытая область.', 'evidence_frame_ids': ids(frames, 3, 5)}]
    session.mode = mode
    session.asset = SimpleNamespace(duration_ms=10_000)
    result = action_evidence.normalize(value, frames, session)
    assert len(result['warnings']) == 30
    codes = {warning['code'] for warning in result['warnings']}
    assert {'sampled_frames', 'approximate_timing', 'occluded_view'} <= codes
    assert ('preliminary_live' if mode == 'live' else 'partial_video') in codes


def test_model_cannot_supply_instruction_or_server_timing_fields(frames, session):
    for field, data in [('steps', []), ('start_ms', 0)]:
        value = payload(frames)
        value[field] = data
        with pytest.raises(InvalidModelOutput, match='VISUAL_OUTPUT_INVALID'):
            action_evidence.normalize(value, frames, session)
    value = payload(frames)
    value['observations'][0]['start_ms'] = 0
    with pytest.raises(InvalidModelOutput, match='VISUAL_OUTPUT_INVALID'):
        action_evidence.normalize(value, frames, session)
