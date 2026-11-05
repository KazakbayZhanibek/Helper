"""Local fixtures for fact consistency, never a claim of visual recognition."""
from copy import deepcopy
import json
from types import SimpleNamespace
import uuid

import pytest

from ai_drafts.validation import InvalidModelOutput
from visual_drafts import grounding, short_actions
from .test_action_protocol import images


def session(language='ru'):
    return SimpleNamespace(mode='upload', language=language, asset=None,
                           window_start_ms=0, window_end_ms=12000,
                           analysis_budget={'visual_protocol': '3', 'short_action_coverage': 'v1', 'frame_grounding': 'v1'})


def frames(source):
    return [SimpleNamespace(id=uuid.UUID(item['id']), sequence=item['sequence'],
                            timestamp_ms=item['timestamp_ms'], coverage_start_ms=item['timestamp_ms'],
                            coverage_end_ms=item['timestamp_ms'] + 500) for item in source]


def wire(source, language='ru'):
    states = []
    for index, image in enumerate(source):
        for offset, entity in enumerate(('e1', 'e2', 'e3')):
            relation = []
            if entity == 'e1':
                relation = [{'kind': 'held_by' if index == 0 else 'supported_by',
                             'target_entity_id': 'e2' if index == 0 else 'e3', 'status': 'observed'}]
            states.append({'id': f's{index * 3 + offset + 1}', 'frame_id': image['id'], 'entity_id': entity,
                           'visibility': 'visible', 'relations': relation})
    refs = [item['id'] for item in source]
    return {
        'schema_version': grounding.WIRE_VERSION, 'language': language,
        'entities': [{'id': 'e1', 'label': 'Заготовка'}, {'id': 'e2', 'label': 'Рука'}, {'id': 'e3', 'label': 'Поверхность'}],
        'frame_states': states,
        'transitions': [{'pair_index': i, 'change': 'visible_action',
                         'description': 'Рука опускает удерживаемую заготовку на поверхность.', 'observation_ids': ['o1']}
                        for i in range(len(source) - 1)],
        'observations': [{'id': 'o1', 'entity_id': 'e1', 'kind': 'action', 'title': 'Опустите заготовку',
                          'movement': 'Удерживайте заготовку рукой и опустите её на поверхность.',
                          'before_state_id': 's1', 'after_state_id': f's{(len(source) - 1) * 3 + 1}',
                          'action_requirements': [
                              {'state_id': 's1', 'kind': 'held_by', 'target_entity_id': 'e2'},
                              {'state_id': f's{(len(source) - 1) * 3 + 1}', 'kind': 'supported_by', 'target_entity_id': 'e3'},
                          ],
                          'action_frame_ids': list(refs), 'evidence_frame_ids': list(refs)}],
        'gaps': [], 'warnings': [],
    }


def normalize(value, source, language='ru'):
    return grounding.normalize_grounded_actions(value, frames(source), session(language))


def test_visible_contact_placement_derives_phases_without_second_prose_claim():
    source = images(3)
    value = wire(source)
    original = deepcopy(value)
    result = normalize(value, source)
    assert value == original
    assert result['schema_version'] == '3' and result['step_format'] == 'teaching_v1'
    assert len(result['steps']) == 1
    observation = result['observations'][0]
    assert observation['subject'] == 'Заготовка'
    assert 'Рука' in observation['before'] and 'Удержание' in observation['before']
    assert 'Поверхность' in observation['after'] and 'Опора' in observation['after']
    assert observation['before_frame_id'] == source[0]['id']
    assert observation['after_frame_id'] == source[-1]['id']
    assert result['steps'][0]['description'].find(observation['after']) >= 0
    ledger = result['frame_grounding']
    assert ledger['version'] == 'v1' and len(ledger['batches']) == 1
    batch = ledger['batches'][0]
    assert batch['states'] == value['frame_states']
    assert batch['frame_ids'] == [item['id'] for item in source]
    assert batch['observation_links'][0]['observation_id'] == observation['id']
    assert batch['rejected_observation_ids'] == []


@pytest.mark.parametrize('language', ['ru', 'kk', 'mixed'])
def test_negative_contact_is_unconfirmed_not_a_claim_of_absence(language):
    source = images(2)
    value = wire(source, language)
    value['frame_states'][0]['relations'][0]['status'] = 'not_observed'
    value['observations'][0].update(kind='state', movement=None, action_frame_ids=[], action_requirements=[])
    result = normalize(value, source, language)
    text = result['observations'][0]['before']
    assert ('расталмаған' if language == 'kk' else 'не подтверждено') in text
    assert 'не касается' not in text and 'не удерживает' not in text


def test_absent_context_before_visible_introduction_remains_conservative():
    source = images(3)
    value = wire(source)
    value['frame_states'][0].update(visibility='absent', relations=[])
    value['frame_states'][3]['relations'] = [{'kind': 'held_by', 'target_entity_id': 'e2', 'status': 'observed'}]
    value['observations'][0]['action_frame_ids'] = [item['id'] for item in source[1:]]
    value['observations'][0]['action_requirements'][0]['state_id'] = 's4'
    value['transitions'][0].update(change='unexplained_change', observation_ids=[])
    result = normalize(value, source)
    assert len(result['steps']) == 1
    assert 'не подтверждено' in result['observations'][0]['before']
    assert result['observations'][0]['before_frame_id'] == source[0]['id']


@pytest.mark.parametrize('visibility', ['absent', 'unclear'])
@pytest.mark.parametrize('position', [0, 1, 2])
def test_absent_or_unclear_subject_in_any_action_frame_is_neutral_question(visibility, position):
    source = images(3)
    value = wire(source)
    value['frame_states'][position * 3].update(visibility=visibility, relations=[])
    result = normalize(value, source)
    assert result['steps'] == result['observations'] == []
    assert result['frame_grounding']['batches'][0]['states'] == value['frame_states']
    assert len(result['frame_grounding']['batches'][0]['rejected_observation_ids']) == 1
    assert all(gap['description'] == grounding.GROUNDING_REVIEW_REASON for gap in result['gaps'])
    assert all(not row['observation_ids'] for row in result['temporal_coverage']['pairs'])
    assert result['temporal_coverage']['unresolved_pairs'] == 2


@pytest.mark.parametrize('failure', ['missing_target', 'absent_target', 'unclear_target', 'later_target'])
def test_positive_relation_needs_both_entities_on_exact_frame(failure):
    source = images(3)
    value = wire(source)
    # The first frame claims holding by e2; only an e2 fact on this frame counts.
    if failure in {'missing_target', 'later_target'}:
        value['frame_states'] = [s for s in value['frame_states'] if s['id'] != 's2']
        if failure == 'missing_target':
            value['frame_states'] = [s for s in value['frame_states'] if s['entity_id'] != 'e2']
    else:
        value['frame_states'][1]['visibility'] = failure.split('_')[0]
    result = normalize(value, source)
    assert not result['steps'] and not result['observations']
    assert result['gaps'] and result['gaps'][0]['description'] == grounding.GROUNDING_REVIEW_REASON


def test_observed_separation_and_support_on_same_frame_cannot_both_teach_action():
    source = images(2)
    value = wire(source)
    value['frame_states'][3]['relations'].append({'kind': 'separated_from', 'target_entity_id': 'e3', 'status': 'observed'})
    result = normalize(value, source)
    assert result['steps'] == []


def test_reverse_entity_contact_contradiction_is_also_checked():
    source = images(2)
    value = wire(source)
    value['frame_states'][5]['relations'].append({'kind': 'separated_from', 'target_entity_id': 'e1', 'status': 'observed'})
    assert normalize(value, source)['steps'] == []


@pytest.mark.parametrize('mutation', [
    'foreign_frame', 'unknown_entity', 'unknown_target', 'unknown_before', 'wrong_before_entity',
    'duplicate_entity', 'duplicate_state_id', 'duplicate_frame_entity', 'duplicate_relation',
    'self_relation', 'empty_entity_label', 'phase_outside_evidence', 'reversed_phases', 'duplicate_observation',
])
def test_invalid_references_are_rejected_even_when_action_would_be_filtered(mutation):
    source = images(3)
    value = wire(source)
    value['frame_states'][0].update(visibility='unclear', relations=[])
    if mutation == 'foreign_frame':
        value['frame_states'][1]['frame_id'] = str(uuid.uuid4())
    elif mutation == 'unknown_entity':
        value['observations'][0]['entity_id'] = 'e8'
    elif mutation == 'unknown_target':
        value['frame_states'][3]['relations'][0]['target_entity_id'] = 'e8'
    elif mutation == 'unknown_before':
        value['observations'][0]['before_state_id'] = 's48'
    elif mutation == 'wrong_before_entity':
        value['observations'][0]['before_state_id'] = 's2'
    elif mutation == 'duplicate_entity':
        value['entities'].append(deepcopy(value['entities'][0]))
    elif mutation == 'duplicate_state_id':
        value['frame_states'][1]['id'] = 's1'
    elif mutation == 'duplicate_frame_entity':
        value['frame_states'][1]['entity_id'] = 'e1'
    elif mutation == 'duplicate_relation':
        value['frame_states'][3]['relations'].append({**value['frame_states'][3]['relations'][0], 'status': 'not_observed'})
    elif mutation == 'self_relation':
        value['frame_states'][3]['relations'][0]['target_entity_id'] = 'e1'
    elif mutation == 'empty_entity_label':
        value['entities'][0]['label'] = ' '
    elif mutation == 'phase_outside_evidence':
        value['observations'][0]['evidence_frame_ids'] = [item['id'] for item in source[1:]]
    elif mutation == 'reversed_phases':
        value['observations'][0].update(before_state_id='s7', after_state_id='s1')
    elif mutation == 'duplicate_observation':
        value['observations'].append(deepcopy(value['observations'][0]))
    with pytest.raises(InvalidModelOutput):
        normalize(value, source)


@pytest.mark.parametrize('corrupt', ['foreign_action', 'cut'])
def test_semantic_filter_cannot_hide_foreign_action_refs_or_cut(corrupt):
    source = images(3)
    value = wire(source)
    value['frame_states'][0].update(visibility='unclear', relations=[])
    if corrupt == 'foreign_action':
        value['observations'][0]['action_frame_ids'][0] = str(uuid.uuid4())
    else:
        value['transitions'][0].update(change='cut', observation_ids=[])
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_UNKNOWN|VISUAL_EVIDENCE_CUT_INVALID'):
        normalize(value, source)


def test_missing_action_state_is_not_implicitly_visible():
    source = images(3)
    value = wire(source)
    value['frame_states'] = [state for state in value['frame_states'] if state['id'] != 's4']
    assert normalize(value, source)['steps'] == []


def test_ledger_and_rejections_survive_idempotent_read_without_mutating_original():
    source = images(3)
    value = wire(source)
    value['frame_states'][3].update(visibility='absent', relations=[])
    result = normalize(value, source)
    original = deepcopy(result)
    once = grounding.reconcile_grounding(result, frames(source), session())
    twice = grounding.reconcile_grounding(once, frames(source), session())
    assert once == twice == original == result


@pytest.mark.parametrize('tamper', ['phase_text', 'phase_frame', 'subject', 'batch_hash', 'unused_foreign_state', 'cross_entity_phase', 'extra_link', 'missing_link'])
def test_read_apply_projection_revalidates_ledger_and_derived_phases(tamper):
    source = images(3)
    result = normalize(wire(source), source)
    batch = result['frame_grounding']['batches'][0]
    if tamper == 'phase_text':
        result['observations'][0]['before'] = 'Предмет уже лежит на поверхности.'
    elif tamper == 'phase_frame':
        result['observations'][0]['before_frame_id'] = source[1]['id']
    elif tamper == 'subject':
        result['observations'][0]['subject'] = 'Другой предмет'
    elif tamper == 'batch_hash':
        batch['id'] = 'b' + '0' * 32
    elif tamper == 'unused_foreign_state':
        batch['states'][2]['frame_id'] = str(uuid.uuid4())
    elif tamper == 'cross_entity_phase':
        batch['observation_links'][0]['before_state_id'] = 's2'
    elif tamper == 'extra_link':
        batch['observation_links'].append(deepcopy(batch['observation_links'][0]))
    elif tamper == 'missing_link':
        batch['observation_links'] = []
    with pytest.raises(InvalidModelOutput):
        grounding.reconcile_grounding(result, frames(source), session())


def test_rejected_identity_cannot_be_resurrected_even_with_formally_valid_action():
    source = images(2)
    result = normalize(wire(source), source)
    identity = result['observations'][0]['id']
    result['frame_grounding']['batches'][0]['rejected_observation_ids'] = [identity]
    checked = grounding.reconcile_grounding(result, frames(source), session())
    assert checked['steps'] == checked['observations'] == []
    assert checked['frame_grounding']['batches'][0]['rejected_observation_ids'] == [identity]
    assert grounding.reconcile_grounding(checked, frames(source), session()) == checked


def test_non_action_state_does_not_become_instruction():
    source = images(2)
    value = wire(source)
    value['observations'][0].update(kind='state', movement=None, action_frame_ids=[], action_requirements=[])
    result = normalize(value, source)
    assert result['steps'] == [] and len(result['observations']) == 1
    assert result['observations'][0]['kind'] == 'state'


def test_long_labels_keep_whole_phase_phrases_bounded_and_full_ledger():
    source = images(2)
    value = wire(source)
    value['entities'][1]['label'] = 'Р' * 80
    value['entities'][2]['label'] = 'П' * 80
    value['frame_states'][0]['relations'].extend([
        {'kind': 'touching', 'target_entity_id': 'e3', 'status': 'not_observed'},
        {'kind': 'supported_by', 'target_entity_id': 'e3', 'status': 'unclear'},
        {'kind': 'separated_from', 'target_entity_id': 'e3', 'status': 'unclear'},
    ])
    result = normalize(value, source)
    before = result['observations'][0]['before']
    assert len(before) <= 240 and before.endswith('.')
    assert result['frame_grounding']['batches'][0]['states'] == value['frame_states']
    assert '…' not in before


def test_two_window_aggregate_keeps_local_fact_ids_scoped_to_batch():
    first, second = images(2), images(2)
    for item in second:
        item['sequence'] += 2
        item['timestamp_ms'] += 2000
    left, right = normalize(wire(first), first), normalize(wire(second), second)
    combined = deepcopy(left)
    for name in ('steps', 'observations', 'gaps'):
        combined[name] += right[name]
    combined['frame_grounding']['batches'] += right['frame_grounding']['batches']
    combined['temporal_coverage']['pairs'] = [
        {**row, 'window_id': str(index)} for index, part in enumerate((left, right))
        for row in part['temporal_coverage']['pairs']]
    checked = grounding.reconcile_grounding(combined, frames(first + second), session())
    assert len(checked['steps']) == len(checked['frame_grounding']['batches']) == 2
    assert checked['observations'][0]['id'] != checked['observations'][1]['id']
    cross_link = deepcopy(checked)
    cross_link['temporal_coverage']['pairs'][0]['observation_ids'].append(checked['observations'][1]['id'])
    with pytest.raises(InvalidModelOutput, match='VISUAL_GROUNDING_REFERENCE_INVALID'):
        grounding.reconcile_grounding(cross_link, frames(first + second), session())
    # A globally available frame still cannot masquerade as a fact in another window.
    checked['frame_grounding']['batches'][0]['states'][0]['frame_id'] = second[0]['id']
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_UNKNOWN'):
        grounding.reconcile_grounding(checked, frames(first + second), session())


def test_legacy_content_is_not_mislabelled_as_grounded():
    legacy = {'schema_version': '3', 'steps': [], 'observations': [], 'gaps': [], 'warnings': []}
    old_session = session()
    old_session.analysis_budget.pop('frame_grounding')
    assert grounding.reconcile_grounding(legacy, [], old_session) == legacy
    with pytest.raises(InvalidModelOutput, match='VISUAL_GROUNDING_MISSING'):
        grounding.reconcile_grounding(legacy, [], session())


def test_empty_failed_window_aggregate_has_no_claims_or_fake_verification():
    empty = {'schema_version': '3', 'steps': [], 'observations': [], 'gaps': [], 'warnings': [],
             'frame_grounding': {'version': 'v1', 'batches': []}}
    assert grounding.reconcile_grounding(empty, [], session()) == empty
    empty['observations'] = [{}]
    with pytest.raises(InvalidModelOutput, match='VISUAL_GROUNDING_MISSING'):
        grounding.reconcile_grounding(empty, [], session())


def test_extra_free_phase_fields_cannot_override_ledger():
    source = images(2)
    value = wire(source)
    value['observations'][0]['after'] = 'Нож касается предмета.'
    with pytest.raises(InvalidModelOutput, match='VISUAL_OUTPUT_INVALID'):
        normalize(value, source)


def test_existing_32kib_output_ceiling_is_not_increased():
    source = images(2)
    value = wire(source)
    value['warnings'] = [{'code': 'uncertain_action', 'message': 'я' * 600} for _ in range(30)]
    assert len(json.dumps(value, ensure_ascii=False).encode()) > 32768
    with pytest.raises(InvalidModelOutput, match='VISUAL_OUTPUT_INVALID'):
        normalize(value, source)


def test_model_schema_does_not_mutate_existing_contract():
    assert 'entities' not in short_actions.SCHEMA['properties']
    assert 'subject' in short_actions.SCHEMA['properties']['observations']['items']['properties']
    assert 'subject' not in grounding.SCHEMA['properties']['observations']['items']['properties']


@pytest.mark.parametrize('status', ['not_observed', 'unclear', 'missing'])
@pytest.mark.parametrize('state_index', [0, 3])
def test_visible_subject_does_not_justify_command_without_observed_method_requirement(status, state_index):
    source = images(2)
    value = wire(source)
    if status == 'missing':
        value['frame_states'][state_index]['relations'] = []
    else:
        value['frame_states'][state_index]['relations'][0]['status'] = status
    result = normalize(value, source)
    assert all(state['visibility'] == 'visible' for state in value['frame_states'])
    assert result['steps'] == result['observations'] == []
    assert all(gap['description'] == grounding.GROUNDING_REVIEW_REASON for gap in result['gaps'])
    batch = result['frame_grounding']['batches'][0]
    assert batch['observation_links'][0]['action_requirements'] == value['observations'][0]['action_requirements']
    assert grounding.reconcile_grounding(result, frames(source), session()) == result


@pytest.mark.parametrize('requirements', ['empty', 'one_frame', 'missing_first', 'missing_last'])
def test_method_requirements_must_cover_both_movement_endpoints(requirements):
    source = images(3)
    value = wire(source)
    action = value['observations'][0]
    if requirements == 'empty':
        action['action_requirements'] = []
    elif requirements == 'one_frame':
        action['action_requirements'] = action['action_requirements'][:1]
    elif requirements == 'missing_first':
        action['action_requirements'][0] = {'state_id': 's4', 'kind': 'supported_by', 'target_entity_id': 'e3'}
    else:
        action['action_requirements'][-1]['state_id'] = 's4'
    assert normalize(value, source)['steps'] == []


@pytest.mark.parametrize('corrupt', ['unknown_state', 'wrong_subject', 'unknown_target', 'self_target', 'outside_action', 'duplicate', 'state_with_requirements'])
def test_method_requirement_references_fail_closed_before_semantic_filter(corrupt):
    source = images(3)
    value = wire(source)
    action = value['observations'][0]
    value['frame_states'][3]['visibility'] = 'unclear'  # Never hides corrupt references.
    if corrupt == 'unknown_state':
        action['action_requirements'][0]['state_id'] = 's48'
    elif corrupt == 'wrong_subject':
        action['action_requirements'][0]['state_id'] = 's2'
    elif corrupt == 'unknown_target':
        action['action_requirements'][0]['target_entity_id'] = 'e8'
    elif corrupt == 'self_target':
        action['action_requirements'][0]['target_entity_id'] = 'e1'
    elif corrupt == 'outside_action':
        action['action_frame_ids'] = [item['id'] for item in source[1:]]
    elif corrupt == 'duplicate':
        action['action_requirements'].append(deepcopy(action['action_requirements'][0]))
    else:
        action.update(kind='state', action_frame_ids=[], movement=None)
    with pytest.raises(InvalidModelOutput, match='VISUAL_GROUNDING_REFERENCE_INVALID'):
        normalize(value, source)


@pytest.mark.parametrize('corrupt', ['missing', 'wrong_entity', 'foreign_state', 'outside_action'])
def test_read_apply_gate_checks_declared_method_requirements_again(corrupt):
    source = images(3)
    result = normalize(wire(source), source)
    link = result['frame_grounding']['batches'][0]['observation_links'][0]
    if corrupt == 'missing':
        # Missing declarations cannot leave a command alive, even if phases match.
        link['action_requirements'] = []
        checked = grounding.reconcile_grounding(result, frames(source), session())
        assert checked['steps'] == []
        assert grounding.reconcile_grounding(checked, frames(source), session()) == checked
        return
    if corrupt == 'wrong_entity':
        link['action_requirements'][0]['state_id'] = 's2'
    elif corrupt == 'foreign_state':
        link['action_requirements'][0]['state_id'] = 's48'
    else:
        # A valid fact in the batch cannot justify a different movement range.
        for item in result['observations']:
            item['action_frame_ids'] = [source[1]['id'], source[2]['id']]
    with pytest.raises(InvalidModelOutput, match='VISUAL_GROUNDING_REFERENCE_INVALID'):
        grounding.reconcile_grounding(result, frames(source), session())
