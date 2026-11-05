"""Inline evidence regression fixtures: consistency checks, not recognition."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from ai_drafts.validation import InvalidModelOutput
from visual_drafts import grounding_v2
from .test_action_protocol import images
from .test_frame_grounding import frames


def session(language='ru'):
    return SimpleNamespace(mode='upload', language=language, asset=None,
                           window_start_ms=0, window_end_ms=12000,
                           analysis_budget={'visual_protocol': '3', 'short_action_coverage': 'v1', 'frame_grounding': 'v2'})


def wire(source, language='ru'):
    evidence = []
    for index in range(1, len(source) + 1):
        evidence.append({'image_index': index, 'subject_visibility': 'visible', 'relations': [{
            'kind': 'held_by' if index < len(source) else 'supported_by',
            'target': 'Рука' if index < len(source) else 'Поверхность',
            'target_visibility': 'visible', 'status': 'observed',
        }]})
    return {'schema_version': grounding_v2.WIRE_VERSION, 'language': language,
            'actions': [{'subject': 'Заготовка', 'title': 'Опустите заготовку',
                         'movement': 'Удерживайте заготовку рукой и опустите её на поверхность.',
                         'motion': 'observed', 'evidence': evidence}], 'cut_after_image_indices': []}


def normalize(value, source, language='ru'):
    return grounding_v2.normalize_grounded_actions(value, frames(source), session(language))


@pytest.mark.parametrize('language', ['ru', 'kk', 'mixed'])
def test_phases_and_real_photo_references_are_derived_without_free_state_text(language):
    source = images(3)
    value = wire(source, language)
    snapshot = deepcopy(value)
    result = normalize(value, source, language)
    assert value == snapshot
    assert result['schema_version'] == '3' and result['step_format'] == 'teaching_v1'
    assert len(result['steps']) == 1
    assert 'insufficient_evidence' not in {warning['code'] for warning in result['warnings']}
    observation = result['observations'][0]
    assert observation['subject'] == value['actions'][0]['subject']
    assert observation['movement'] == value['actions'][0]['movement']
    assert 'Рука' in observation['before'] and 'Поверхность' in observation['after']
    assert observation['before_frame_id'] == source[0]['id']
    assert observation['after_frame_id'] == source[-1]['id']
    assert result['steps'][0]['evidence_frame_ids'] == [image['id'] for image in source]
    assert result['frame_grounding']['batches'][0]['actions'] == value['actions']
    assert grounding_v2.reconcile_grounding(result, frames(source), session(language)) == result


def test_two_relations_on_one_image_are_not_two_interaction_images():
    source = images(2)
    value = wire(source)
    value['actions'][0]['evidence'][0]['relations'].append({
        'kind': 'touching', 'target': 'Инструмент', 'target_visibility': 'visible', 'status': 'observed'})
    value['actions'][0]['evidence'][1]['relations'] = []
    result = normalize(value, source)
    assert result['steps'] == result['observations'] == []
    assert result['frame_grounding']['batches'][0]['actions'] == value['actions']
    assert all(gap['description'] == grounding_v2.GROUNDING_REVIEW_REASON for gap in result['gaps'])


def test_multiple_required_relations_on_each_frame_are_retained_in_ledger():
    source = images(2)
    value = wire(source)
    for row in value['actions'][0]['evidence']:
        row['relations'].append({'kind': 'touching', 'target': 'Инструмент',
                                 'target_visibility': 'visible', 'status': 'observed'})
    result = normalize(value, source)
    assert len(result['steps']) == 1
    assert 'Инструмент' in result['observations'][0]['before']
    assert result['frame_grounding']['batches'][0]['actions'] == value['actions']


def test_contact_and_separation_of_same_target_reject_whole_action():
    source = images(2)
    value = wire(source)
    value['actions'][0]['evidence'][0]['relations'].append({
        'kind': 'separated_from', 'target': 'Рука', 'target_visibility': 'visible', 'status': 'observed'})
    assert not normalize(value, source)['steps']


@pytest.mark.parametrize('mutation', ['duplicate_relation', 'self_relation', 'blank_target', 'new_field', 'wrong_language'])
def test_malformed_fact_inventory_rejects_even_when_motion_unknown(mutation):
    source = images(2)
    value = wire(source)
    action = value['actions'][0]
    action['motion'] = 'unclear'
    relation = action['evidence'][0]['relations'][0]
    if mutation == 'duplicate_relation':
        action['evidence'][0]['relations'].append(deepcopy(relation))
    elif mutation == 'self_relation':
        relation['target'] = action['subject']
    elif mutation == 'blank_target':
        relation['target'] = '  '
    elif mutation == 'new_field':
        action['before'] = 'Выдуманная подготовка'
    else:
        value['language'] = 'kk'
    with pytest.raises(InvalidModelOutput):
        normalize(value, source)


def test_all_uncovered_pairs_are_neutral_review_evidence_not_invented_actions():
    source = images(4)
    value = wire(source)
    value['actions'] = []
    value['cut_after_image_indices'] = [2]
    result = normalize(value, source)
    assert result['steps'] == []
    assert len(result['temporal_coverage']['pairs']) == 3
    assert [row['change'] for row in result['temporal_coverage']['pairs']] == ['unexplained_change', 'cut', 'unexplained_change']
    assert result['temporal_coverage']['unresolved_pairs'] == 2
    assert len(result['gaps']) == 3
    assert grounding_v2.reconcile_grounding(result, frames(source), session()) == result


def test_rejected_card_and_all_its_facts_survive_idempotent_read_projection():
    source = images(3)
    value = wire(source)
    value['actions'][0]['evidence'][1]['relations'][0]['status'] = 'not_observed'
    result = normalize(value, source)
    assert not result['steps']
    assert result['frame_grounding']['batches'][0]['actions'] == value['actions']
    for _ in range(3):
        assert grounding_v2.reconcile_grounding(result, frames(source), session()) == result


def test_previously_removed_action_is_not_resurrected_by_read_projection():
    source = images(2)
    result = normalize(wire(source), source)
    identity = result['observations'][0]['id']
    result['frame_grounding']['batches'][0]['rejected_observation_ids'] = [identity]
    with pytest.raises(InvalidModelOutput):
        grounding_v2.reconcile_grounding(result, frames(source), session())


@pytest.mark.parametrize('field', ['title', 'movement', 'subject', 'before', 'after', 'action_frame_ids', 'start_ms'])
def test_normalized_action_cannot_diverge_from_stored_source_card(field):
    source = images(3)
    result = normalize(wire(source), source)
    if field == 'action_frame_ids':
        result['observations'][0][field] = [source[0]['id'], source[1]['id']]
    elif field == 'start_ms':
        result['observations'][0][field] += 1
    else:
        result['observations'][0][field] = 'Добавленная неподтверждённая команда'
    with pytest.raises(InvalidModelOutput):
        grounding_v2.reconcile_grounding(result, frames(source), session())


def test_pair_links_cannot_be_changed_into_a_separate_model_confirmation():
    source = images(2)
    result = normalize(wire(source), source)
    result['temporal_coverage']['pairs'][0]['description'] = 'Независимо подтверждено видео'
    with pytest.raises(InvalidModelOutput):
        grounding_v2.reconcile_grounding(result, frames(source), session())


def test_rejected_card_review_gap_cannot_be_rebound_to_other_valid_frames():
    source = images(3)
    value = wire(source)
    value['actions'][0]['motion'] = 'unclear'
    result = normalize(value, source)
    gap = result['gaps'][0]
    gap['evidence_frame_ids'] = [image['id'] for image in source[:2]]
    gap['end_ms'] = source[1]['timestamp_ms'] + 500
    with pytest.raises(InvalidModelOutput):
        grounding_v2.reconcile_grounding(result, frames(source), session())


def test_empty_failed_aggregate_is_valid_only_without_claims_or_pairs():
    result = {'schema_version': '3', 'language': 'ru', 'steps': [], 'observations': [], 'gaps': [],
              'warnings': [], 'frame_grounding': {'version': 'v2', 'batches': []}}
    assert grounding_v2.reconcile_grounding(result, [], session()) == result
    result['steps'] = [{'title': 'Неподтверждённая команда'}]
    with pytest.raises(InvalidModelOutput):
        grounding_v2.reconcile_grounding(result, [], session())


def test_schema_indices_are_request_scoped_without_mutating_unbound_schema():
    source = images(3)
    before = deepcopy(grounding_v2.SCHEMA)
    bound = grounding_v2.bind_schema(grounding_v2.SCHEMA, source)
    assert grounding_v2.SCHEMA == before
    assert bound['properties']['actions']['items']['properties']['evidence']['items']['properties']['image_index']['enum'] == [1, 2, 3]
    assert bound['properties']['cut_after_image_indices']['items']['enum'] == [1, 2]
    assert len((json.dumps(bound, ensure_ascii=False) + grounding_v2.SYSTEM).encode()) < 16000


def test_wire_byte_budget_is_checked_before_semantic_drops():
    source = images(12)
    value = wire(source)
    action = value['actions'][0]
    action['motion'] = 'unclear'
    action['movement'] = 'Ж' * 800
    value['actions'] = [deepcopy(action) for _ in range(12)]
    assert len(json.dumps(value, ensure_ascii=False).encode()) > 32768
    with pytest.raises(InvalidModelOutput):
        normalize(value, source)


def test_internal_uuid_expansion_does_not_shrink_the_wire_budget():
    source = images(12)
    value = wire(source)
    action = value['actions'][0]
    action['movement'] = 'Move object. ' * 60
    action['evidence'] = [dict(row, relations=[]) for row in action['evidence']]
    for row in (action['evidence'][0], action['evidence'][-1]):
        row['relations'] = [{'kind': 'held_by', 'target': 'Hand', 'target_visibility': 'visible', 'status': 'observed'}]
    value['actions'] = [deepcopy(action) for _ in range(12)]
    assert len(json.dumps(value, ensure_ascii=False).encode()) < 32768
    result = normalize(value, source)
    assert len(result['steps']) == len(result['observations']) == 12
    assert grounding_v2.reconcile_grounding(result, frames(source), session()) == result
