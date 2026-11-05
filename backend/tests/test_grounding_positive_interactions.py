"""Contact quorum regression checks; no pixel or model-accuracy assertions."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import pytest

from ai_budget.models import AIUsageReservation
from ai_drafts import codex_provider
from ai_drafts.validation import InvalidModelOutput
from visual_drafts import grounding_v2, planning, provider, services
from visual_drafts.models import VisualProposal, VisualSession
from .conftest import post
from .test_action_protocol import images
from .test_frame_grounding import frames as trusted_frames
from .test_grounding_v2_adversarial import action, session, wire
from .test_instruction_photos import evidence as saved_evidence


@pytest.fixture(autouse=True)
def forbid_model_transport(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Contact quorum validation must not invoke any model')

    monkeypatch.setattr(provider.VisualProvider, 'generate', forbidden)
    monkeypatch.setattr(provider, 'json_request', forbidden)
    monkeypatch.setattr(codex_provider, 'generate', forbidden)


def separated(indices=(1, 2)):
    return action(indices, relation_kind='separated_from', target='Стол',
                  title='Поверните деталь', movement='Поверните деталь щипцами по часовой стрелке.')


def mixed_wire():
    return wire(separated(), action((3, 4), subject='Ткань', target='Пальцы',
                                   title='Переместите ткань', movement='Переместите ткань пальцами.'))


def legacy(value, frames, selected=None):
    """Recreate the old relation-count gate, keeping all structural checks."""
    return grounding_v2._build_content(value, frames, selected or session(), check_interactions=False)


def assert_filtered(content, frames):
    assert len(content['steps']) == len(content['observations']) == 1
    assert content['observations'][0]['subject'] == 'Ткань'
    assert content['steps'][0]['evidence_frame_ids'] == [str(frame.id) for frame in frames[2:]]
    assert content['temporal_coverage']['pairs'][0]['change'] == 'unexplained_change'
    assert content['temporal_coverage']['pairs'][0]['observation_ids'] == []
    assert content['temporal_coverage']['pairs'][-1]['status'] == 'linked_action'
    assert any(gap['description'] == grounding_v2.GROUNDING_REVIEW_REASON for gap in content['gaps'])
    assert all('щипцами' not in gap['description'] for gap in content['gaps'])


@pytest.mark.parametrize('positive_count', [0, 1])
def test_separation_alone_or_from_an_unrelated_target_cannot_supply_interaction(positive_count):
    frames = trusted_frames(images(3))
    value = separated((1, 2, 3))
    if positive_count:
        value['evidence'][1]['relations'] = [
            {'kind': 'held_by', 'target': 'Рука', 'target_visibility': 'visible', 'status': 'observed'},
            {'kind': 'touching', 'target': 'Щипцы', 'target_visibility': 'visible', 'status': 'observed'},
        ]
    original = wire(value)
    snapshot = deepcopy(original)
    assert legacy(original, frames)['steps']
    content = grounding_v2.normalize_grounded_actions(original, frames, session())
    assert content['steps'] == content['observations'] == []
    assert content['frame_grounding']['batches'][0]['actions'] == original['actions']
    assert original == snapshot
    assert grounding_v2.reconcile_grounding(content, frames, session()) == content


@pytest.mark.parametrize('kind', ['touching', 'held_by', 'supported_by'])
def test_two_positive_frames_keep_separation_as_result_evidence(kind):
    frames = trusted_frames(images(4))
    value = action((1, 2, 3, 4), relation_kind=kind, target='Инструмент')
    for index in (0, 3):
        value['evidence'][index]['relations'][0]['kind'] = 'separated_from'
    content = grounding_v2.normalize_grounded_actions(wire(value), frames, session())
    assert len(content['steps']) == 1
    observation = content['observations'][0]
    assert observation['evidence_frame_ids'] == [str(frame.id) for frame in frames]
    assert 'Раздельное положение' in observation['before']
    assert 'Раздельное положение' in observation['after']
    assert grounding_v2.reconcile_grounding(content, frames, session()) == content


def test_mixed_contact_kinds_on_distinct_frames_satisfy_quorum():
    frames = trusted_frames(images(3))
    value = separated((1, 2, 3))
    value['evidence'][0]['relations'][0].update(kind='held_by', target='Рука')
    value['evidence'][1]['relations'][0].update(kind='supported_by', target='Поверхность')
    assert len(grounding_v2.normalize_grounded_actions(wire(value), frames, session())['steps']) == 1


@pytest.mark.parametrize('kind', ['touching', 'held_by', 'supported_by'])
@pytest.mark.parametrize('release', [False, True])
def test_short_same_target_contact_transition_keeps_both_phases_and_photos(kind, release):
    frames = trusted_frames(images(2))
    value = action(target='Рука', relation_kind=kind)
    value['evidence'][1 if release else 0]['relations'][0].update(
        kind='separated_from', target=' рУкА ')
    original = deepcopy(value)
    content = grounding_v2.normalize_grounded_actions(wire(value), frames, session())
    assert len(content['steps']) == len(content['observations']) == 1
    observation = content['observations'][0]
    assert observation['evidence_frame_ids'] == [str(frame.id) for frame in frames]
    assert observation['before_frame_id'] == str(frames[0].id)
    assert observation['after_frame_id'] == str(frames[1].id)
    assert 'Раздельное положение' in observation['after' if release else 'before']
    assert 'Раздельное положение' not in observation['before' if release else 'after']
    assert content['frame_grounding']['batches'][0]['actions'] == [original]
    assert value == original
    assert grounding_v2.reconcile_grounding(content, frames, session()) == content


@pytest.mark.parametrize('title', ['Отпустите деталь', 'Поднимите деталь', 'Переместите деталь'])
def test_command_words_do_not_substitute_for_or_override_contact_transition(title):
    frames = trusted_frames(images(2))
    value = separated()
    value['title'] = title
    value['evidence'][0]['relations'][0].update(kind='held_by', target='Рука')
    assert not grounding_v2.normalize_grounded_actions(wire(value), frames, session())['steps']
    value['evidence'][1]['relations'][0]['target'] = 'Рука'
    assert len(grounding_v2.normalize_grounded_actions(wire(value), frames, session())['steps']) == 1


@pytest.mark.parametrize('problem', ['motion', 'subject', 'target', 'relation', 'prose'])
def test_contact_transition_does_not_bypass_other_evidence_guards(problem):
    frames = trusted_frames(images(2))
    value = action(target='Рука')
    value['evidence'][1]['relations'][0]['kind'] = 'separated_from'
    if problem == 'motion':
        value['motion'] = 'unclear'
    elif problem == 'subject':
        value['evidence'][1]['subject_visibility'] = 'absent'
    elif problem == 'target':
        value['evidence'][1]['relations'][0]['target_visibility'] = 'unclear'
    elif problem == 'relation':
        value['evidence'][1]['relations'][0]['status'] = 'not_observed'
    else:
        value['movement'] = 'Переместите деталь с усилием 20 Н.'
    assert not grounding_v2.normalize_grounded_actions(wire(value), frames, session())['steps']


def test_contact_and_separation_on_a_single_image_are_not_a_transition():
    frames = trusted_frames(images(2))
    value = action(target='Рука')
    value['evidence'][0]['relations'].append({
        'kind': 'separated_from', 'target': 'Рука', 'target_visibility': 'visible', 'status': 'observed'})
    assert not grounding_v2.normalize_grounded_actions(wire(value), frames, session())['steps']


def test_same_target_transition_can_span_visible_empty_relation_frames():
    frames = trusted_frames(images(4))
    value = action((1, 2, 3, 4), target='Рука')
    for index in (1, 2):
        value['evidence'][index]['relations'] = []
    value['evidence'][3]['relations'][0]['kind'] = 'separated_from'
    content = grounding_v2.normalize_grounded_actions(wire(value), frames, session())
    assert len(content['steps']) == 1
    assert content['observations'][0]['action_frame_ids'] == [str(frame.id) for frame in frames]


def test_fresh_rejection_keeps_supported_sibling_photos_and_raw_cards():
    frames, value = trusted_frames(images(4)), mixed_wire()
    original = deepcopy(value)
    content = grounding_v2.normalize_grounded_actions(value, frames, session())
    assert_filtered(content, frames)
    assert value == original
    assert content['frame_grounding']['batches'][0]['actions'] == original['actions']
    assert grounding_v2.reconcile_grounding(content, frames, session()) == content


def test_legacy_projection_is_filtered_idempotently_without_whole_answer_failure():
    frames = trusted_frames(images(4))
    content = legacy(mixed_wire(), frames)
    original = deepcopy(content)
    assert len(content['steps']) == 2
    once = grounding_v2.reconcile_grounding(content, frames, session())
    assert_filtered(once, frames)
    assert content == original
    assert once['frame_grounding']['batches'][0]['actions'] == original['frame_grounding']['batches'][0]['actions']
    assert len(once['frame_grounding']['batches'][0]['rejected_observation_ids']) == 1
    assert grounding_v2.reconcile_grounding(once, frames, session()) == once


def test_rejected_card_does_not_return_if_quorum_rule_is_relaxed_later(monkeypatch):
    frames = trusted_frames(images(4))
    once = grounding_v2.reconcile_grounding(legacy(mixed_wire(), frames), frames, session())
    monkeypatch.setattr(grounding_v2, '_INTERACTION_KINDS', frozenset(grounding_v2._KINDS))
    assert grounding_v2.reconcile_grounding(once, frames, session()) == once


@pytest.mark.parametrize('corruption', ['raw_index', 'observation_text', 'step_text', 'coverage',
                                       'review_gap_reference'])
def test_new_gate_does_not_hide_corruption_in_the_card_it_will_remove(corruption):
    frames = trusted_frames(images(4))
    content = legacy(mixed_wire(), frames)
    if corruption == 'raw_index':
        content['frame_grounding']['batches'][0]['actions'][0]['evidence'][1]['image_index'] = 99
    elif corruption == 'observation_text':
        content['observations'][0]['movement'] += ' Изменённый метод.'
    elif corruption == 'step_text':
        content['steps'][0]['description'] += ' Изменённый метод.'
    elif corruption == 'coverage':
        content['temporal_coverage']['pairs'][0]['observation_ids'] = []
    else:
        content['gaps'].append({
            'id': content['observations'][0]['id'] + ':grounding', 'reason': 'unclear_action',
            'description': grounding_v2.GROUNDING_REVIEW_REASON,
            'evidence_frame_ids': [str(frame.id) for frame in frames[2:]],
            'start_ms': frames[2].coverage_start_ms, 'end_ms': frames[3].coverage_end_ms,
        })
    original = deepcopy(content)
    with pytest.raises(InvalidModelOutput):
        grounding_v2.reconcile_grounding(content, frames, session())
    assert content == original


def test_multiple_windows_keep_their_boundaries_when_a_card_is_filtered(monkeypatch):
    frames = trusted_frames(images(4))
    first = legacy(wire(separated()), frames[:2])
    second = legacy(wire(action(subject='Ткань', target='Пальцы')), frames[2:])
    windows = [SimpleNamespace(id=uuid.uuid4(), parent_id=None, state='completed', content=content)
               for content in (first, second)]
    monkeypatch.setattr(planning, 'analysis_data', lambda _: {'fixture': 'unchanged'})
    content = planning.aggregate_content(session(), windows)
    snapshot = deepcopy(content)
    once = grounding_v2.reconcile_grounding(content, frames, session())
    assert len(once['steps']) == 1 and once['observations'][0]['subject'] == 'Ткань'
    assert [(pair['window_id'], pair['status']) for pair in once['temporal_coverage']['pairs']] == [
        (str(windows[0].id), 'unresolved'), (str(windows[1].id), 'linked_action')]
    assert once['analysis'] == {'fixture': 'unchanged'}
    assert content == snapshot and grounding_v2.reconcile_grounding(once, frames, session()) == once


def saved_legacy(team, draft, *, all_separated=False):
    create = VisualSession.objects.create
    with patch.object(VisualSession.objects, 'create', side_effect=lambda **kwargs: create(
            **kwargs, analysis_budget=deepcopy(session().analysis_budget))):
        selected, frames = saved_evidence(team, draft, count=4)
    value = mixed_wire()
    if all_separated:
        value['actions'][1] = separated((3, 4))
    item = VisualProposal.objects.create(
        organization=team.org, session=selected, source_job_id=uuid.uuid4(),
        provider='local-test', model='no-model-call', content=legacy(value, frames, selected))
    return item, selected, frames


@pytest.mark.django_db
def test_read_then_apply_filters_legacy_card_and_preserves_sibling_photos(team, draft):
    item, selected, frames = saved_legacy(team, draft)
    snapshot = deepcopy(item.content)
    preview = services.proposal_data(item)
    assert preview == services.proposal_data(item) and preview['can_apply']
    assert_filtered(preview, frames)
    item.refresh_from_db()
    assert item.content == snapshot and item.applied_at is None
    result = post(team.clients['author'], draft.url + f'/visual-proposals/{item.id}/apply',
                  {'confirm_replace': True}, etag=draft.working['etag'], key='contact-safe-apply')
    assert result.status_code == 200, result.json()
    applied = result.json()['steps']
    assert len(applied) == 1
    for field in ('title', 'description', 'start_ms', 'end_ms', 'evidence_frame_ids'):
        assert applied[0][field] == preview['steps'][0][field]
    assert [frame['id'] for frame in applied[0]['evidence_frames']] == [str(frame.id) for frame in frames[2:]]
    item.refresh_from_db()
    assert item.content == snapshot and item.applied_at is not None
    assert not AIUsageReservation.objects.exists()


@pytest.mark.django_db
def test_all_separated_cards_cannot_erase_the_working_instruction(team, draft):
    item, selected, frames = saved_legacy(team, draft, all_separated=True)
    snapshot = deepcopy(item.content)
    working = selected.instruction.working_copy
    revision, steps = working.revision, list(working.steps.order_by('ordinal').values())
    preview = services.proposal_data(item)
    assert preview['steps'] == preview['observations'] == [] and preview['can_apply'] is False
    assert {frame['id'] for frame in preview['frames']} == {str(frame.id) for frame in frames}
    result = post(team.clients['author'], draft.url + f'/visual-proposals/{item.id}/apply',
                  {'confirm_replace': True}, etag=draft.working['etag'], key='contact-empty-apply')
    assert result.status_code == 422 and result.json()['error']['code'] == 'NO_PROPOSED_STEPS'
    item.refresh_from_db()
    working.refresh_from_db()
    assert item.content == snapshot and item.applied_at is None
    assert working.revision == revision and list(working.steps.order_by('ordinal').values()) == steps
    assert not AIUsageReservation.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize('corruption', ['observation_text', 'step_text', 'raw_index', 'coverage'])
def test_tampered_legacy_card_cannot_hide_behind_new_filter_during_apply(team, draft, corruption):
    item, selected, _ = saved_legacy(team, draft)
    working = selected.instruction.working_copy
    revision, steps = working.revision, list(working.steps.order_by('ordinal').values())
    content = deepcopy(item.content)
    if corruption == 'observation_text':
        content['observations'][0]['movement'] += ' Изменённый метод.'
    elif corruption == 'step_text':
        content['steps'][0]['description'] += ' Изменённый метод.'
    elif corruption == 'raw_index':
        content['frame_grounding']['batches'][0]['actions'][0]['evidence'][1]['image_index'] = 99
    else:
        content['temporal_coverage']['pairs'][0]['description'] += ' Подмена.'
    corrupt = VisualProposal.objects.create(
        organization=team.org, session=selected, source_job_id=uuid.uuid4(),
        provider='local-test', model='no-model-call', content=content)
    assert services.proposal_data(corrupt)['can_apply'] is False
    result = post(team.clients['author'], draft.url + f'/visual-proposals/{corrupt.id}/apply',
                  {'confirm_replace': True}, etag=draft.working['etag'], key='contact-corrupt-apply')
    assert result.status_code == 422, result.json()
    corrupt.refresh_from_db()
    working.refresh_from_db()
    assert corrupt.content == content and corrupt.applied_at is None
    assert working.revision == revision and list(working.steps.order_by('ordinal').values()) == steps
    assert not AIUsageReservation.objects.exists()
