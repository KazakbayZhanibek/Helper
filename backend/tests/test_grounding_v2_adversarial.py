"""Independent consistency tests, not evidence of visual recognition quality.

Fixtures intentionally use unrelated domains. All inference stays local.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from ai_drafts.validation import InvalidModelOutput
from visual_drafts import grounding_v2
from .test_action_protocol import images
from .test_frame_grounding import frames as trusted_frames, wire as old_wire


@pytest.fixture(autouse=True)
def no_model_transport(monkeypatch):
    from ai_drafts import codex_provider
    from visual_drafts import provider

    def forbidden(*args, **kwargs):
        pytest.fail('Grounding projection must not invoke a model or paid transport')

    monkeypatch.setattr(provider.VisualProvider, 'generate', forbidden)
    monkeypatch.setattr(provider, 'json_request', forbidden)
    monkeypatch.setattr(codex_provider, 'generate', forbidden)


def session(version='v2'):
    return SimpleNamespace(mode='upload', language='ru', asset=None,
                           window_start_ms=0, window_end_ms=12000,
                           analysis_budget={'visual_protocol': '3', 'short_action_coverage': 'v1',
                                            'frame_grounding': version})


def action(indices=(1, 2), *, subject='Деталь', target='Рука', relation_kind='held_by', title='Переместите деталь',
           movement='Переместите деталь рукой вдоль поверхности.'):
    return {'subject': subject, 'title': title, 'movement': movement, 'motion': 'observed',
            'evidence': [{'image_index': index, 'subject_visibility': 'visible',
                          'relations': [{'kind': relation_kind, 'target': target,
                                         'target_visibility': 'visible', 'status': 'observed'}]}
                         for index in indices]}


def wire(*actions, cuts=()):
    return {'schema_version': '3-grounded-actions-v2', 'language': 'ru',
            'actions': list(actions), 'cut_after_image_indices': list(cuts)}


def normalize(value, source):
    return grounding_v2.normalize_grounded_actions(value, trusted_frames(source), session())


def user_text(content):
    return json.dumps({key: content.get(key) for key in (
        'steps', 'observations', 'gaps', 'warnings', 'temporal_coverage')}, ensure_ascii=False)


@pytest.mark.parametrize('subject,target,kind,title,movement', [
    ('Деталь', 'Рука', 'held_by', 'Переместите деталь', 'Переместите деталь рукой вдоль поверхности.'),
    ('Нить', 'Пальцы', 'held_by', 'Протяните нить', 'Протяните удерживаемую пальцами нить.'),
    ('Ткань', 'Щётка', 'touching', 'Проведите щёткой по ткани', 'Проведите щёткой вдоль ткани.'),
    ('Ёмкость', 'Рука', 'held_by', 'Наклоните ёмкость', 'Наклоните удерживаемую рукой ёмкость.'),
])
def test_supported_action_across_domains_retains_method_and_real_frame_references(subject, target, kind, title, movement):
    source = images(2)
    value = wire(action(subject=subject, target=target, relation_kind=kind, title=title, movement=movement))
    original = deepcopy(value)
    result = normalize(value, source)
    assert value == original
    assert len(result['steps']) == len(result['observations']) == 1
    observation = result['observations'][0]
    assert observation['subject'] == subject and observation['movement'] == movement
    assert observation['evidence_frame_ids'] == [image['id'] for image in source]
    assert observation['action_frame_ids'] == [image['id'] for image in source]
    assert result['frame_grounding']['version'] == 'v2'


@pytest.mark.parametrize('reason', [
    'motion_unclear', 'motion_not_observed', 'subject_unclear', 'subject_absent',
    'target_unclear', 'target_absent', 'relation_unclear', 'relation_not_observed',
    'no_relations', 'one_contact_only', 'additional_unknown_relation',
])
def test_one_unknown_required_component_removes_whole_method_but_retains_supported_sibling(reason):
    source = images(4)
    rejected = action(movement='Контрольный способ: положите и затяните деталь.',
                      title='Контрольная команда')
    positive = normalize(wire(deepcopy(rejected)), source)
    assert len(positive['observations']) == 1
    assert positive['observations'][0]['movement'] == rejected['movement']
    if reason.startswith('motion_'):
        rejected['motion'] = reason.removeprefix('motion_')
    elif reason.startswith('subject_'):
        rejected['evidence'][0]['subject_visibility'] = reason.removeprefix('subject_')
    elif reason.startswith('target_'):
        rejected['evidence'][0]['relations'][0]['target_visibility'] = reason.removeprefix('target_')
    elif reason.startswith('relation_'):
        rejected['evidence'][0]['relations'][0]['status'] = reason.removeprefix('relation_')
    elif reason == 'no_relations':
        for evidence in rejected['evidence']:
            evidence['relations'] = []
    elif reason == 'one_contact_only':
        rejected['evidence'][1]['relations'] = []
    else:
        rejected['evidence'][0]['relations'].append(
            {'kind': 'touching', 'target': 'Ключ', 'target_visibility': 'visible', 'status': 'unclear'})
    good = action((3, 4), subject='Ткань', target='Пальцы', title='Переместите ткань',
                  movement='Переместите ткань пальцами.')
    result = normalize(wire(rejected, good), source)
    assert len(result['steps']) == len(result['observations']) == 1
    assert result['observations'][0]['subject'] == 'Ткань'
    assert 'Контрольный способ' not in user_text(result) and 'Контрольная команда' not in user_text(result)
    assert result['gaps']
    assert grounding_v2.reconcile_grounding(result, trusted_frames(source), session()) == result


@pytest.mark.parametrize('motion', ['unclear', 'not_observed'])
def test_visible_static_contacts_do_not_become_a_pickup_instruction(motion):
    source = images(2)
    value = action(title='Возьмите предмет', movement='Возьмите предмет с поверхности.')
    value['motion'] = motion
    result = normalize(wire(value), source)
    assert result['steps'] == result['observations'] == []
    assert 'Возьмите' not in user_text(result)


@pytest.mark.parametrize('title,movement', [
    ('Сдвиньте кусочки', 'Переместите руку над кусочками, касаясь их; дальнейшее перемещение кусочков не подтверждается.'),
    ('Затяните винт', 'Поверните отвёртку; вращение самого винта не подтверждено.'),
    ('Протрите ткань', 'Проведите щёткой по ткани; движение щётки не различимо.'),
    ('Переместите ёмкость', 'Двигайте ёмкость рукой; движение ёмкости не наблюдается.'),
])
def test_explicit_denial_in_method_overrides_confident_motion_and_contact_flags(title, movement):
    source = images(2)
    result = normalize(wire(action(title=title, movement=movement)), source)
    assert result['steps'] == result['observations'] == []
    assert title not in user_text(result) and movement not in user_text(result)


@pytest.mark.parametrize('movement', [
    'Бөлшекті қолмен жылжытыңыз; оның қозғалысы расталмаған.',
    'Бөлшекті қолмен жылжытыңыз; оның қозғалысы көрінбейді.',
])
def test_explicit_evidence_denial_in_supported_kazakh_language_drops_command(movement):
    source = images(2)
    selected = session()
    selected.language = 'kk'
    value = wire(action(subject='Бөлшек', target='Қол', title='Бөлшекті жылжытыңыз', movement=movement))
    value['language'] = 'kk'
    result = grounding_v2.normalize_grounded_actions(value, trusted_frames(source), selected)
    assert result['steps'] == result['observations'] == []
    assert movement not in user_text(result)


def test_negative_word_alone_is_not_an_evidence_denial():
    source = images(2)
    value = action(relation_kind='supported_by', target='Поверхность',
                   movement='Сдвиньте деталь, не отрывая от поверхности.')
    result = normalize(wire(value), source)
    assert len(result['observations']) == 1
    assert result['observations'][0]['movement'] == value['movement']


def test_contacts_can_be_internal_without_fabricating_contact_at_both_endpoints():
    source = images(4)
    value = action((1, 2, 3, 4))
    value['evidence'][0]['relations'] = []
    value['evidence'][3]['relations'] = []
    result = normalize(wire(value), source)
    assert len(result['observations']) == 1
    assert result['observations'][0]['action_frame_ids'] == [image['id'] for image in source]


@pytest.mark.parametrize('indices', [(0, 1), (1, 13), (1, True), (1, 2.5), (1, '2'),
                                      (1, 1), (2, 1), (1, 3)])
@pytest.mark.parametrize('unsupported', [False, True])
def test_invalid_indices_fail_before_semantic_filtering(indices, unsupported):
    source = images(4)
    bad = action(indices)
    if unsupported:
        bad['motion'] = 'unclear'
    with pytest.raises(InvalidModelOutput):
        normalize(wire(bad, action((3, 4))), source)


@pytest.mark.parametrize('unsupported', [False, True])
def test_cut_crossing_fails_even_if_the_action_would_be_dropped(unsupported):
    source = images(4)
    bad = action()
    if unsupported:
        bad['motion'] = 'unclear'
    with pytest.raises(InvalidModelOutput):
        normalize(wire(bad, action((3, 4)), cuts=(1,)), source)


def test_cut_between_separate_actions_keeps_each_supported_action():
    source = images(4)
    result = normalize(wire(action(), action((3, 4), subject='Ткань'), cuts=(2,)), source)
    assert len(result['observations']) == len(result['steps']) == 2


def test_missing_pair_is_a_gap_without_new_motion_or_command():
    source = images(4)
    result = normalize(wire(action(), action((3, 4), subject='Ткань')), source)
    assert len(result['observations']) == 2
    assert any(gap['evidence_frame_ids'] == [source[1]['id'], source[2]['id']]
               for gap in result['gaps'])


def test_projection_is_idempotent_and_does_not_mutate_saved_content():
    source = images(3)
    result = normalize(wire(action()), source)
    saved = deepcopy(result)
    once = grounding_v2.reconcile_grounding(result, trusted_frames(source), session())
    twice = grounding_v2.reconcile_grounding(once, trusted_frames(source), session())
    assert result == saved and once == twice == result


def test_modified_normalized_command_cannot_bypass_saved_evidence():
    source = images(2)
    result = normalize(wire(action()), source)
    altered = deepcopy(result)
    altered['steps'][0]['description'] = 'ДОБАВЛЕННЫЙ СПОСОБ: нагрейте до неизвестной температуры.'
    altered['observations'][0]['movement'] = 'ДОБАВЛЕННЫЙ СПОСОБ: нагрейте до неизвестной температуры.'
    try:
        projected = grounding_v2.reconcile_grounding(altered, trusted_frames(source), session())
    except InvalidModelOutput:
        return
    assert 'ДОБАВЛЕННЫЙ СПОСОБ' not in user_text(projected)
    assert projected == result


def test_v1_wire_is_not_reinterpreted_as_v2():
    source = images(2)
    with pytest.raises(InvalidModelOutput):
        normalize(old_wire(source), source)


@pytest.mark.parametrize('mode', ['live', 'unknown'])
def test_v2_does_not_turn_live_or_unknown_mode_into_an_instruction(mode):
    source = images(2)
    selected = session()
    selected.mode = mode
    with pytest.raises(InvalidModelOutput):
        grounding_v2.normalize_grounded_actions(wire(action()), trusted_frames(source), selected)
