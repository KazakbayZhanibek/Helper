"""Text-evidence guard integration; no inference and no production database writes."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import pytest

from ai_budget.models import AIUsageReservation
from ai_drafts import codex_provider
from ai_drafts.validation import InvalidModelOutput
from visual_drafts import grounding_v2, planning, prose_evidence, provider, services
from visual_drafts.models import VisualProposal, VisualSession
from .conftest import post
from .test_action_protocol import images
from .test_frame_grounding import frames as trusted_frames
from .test_grounding_v2_adversarial import action, session, wire
from .test_instruction_photos import evidence as saved_evidence


@pytest.fixture(autouse=True)
def forbid_model_transport(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('A local prose projection must not invoke any model')

    monkeypatch.setattr(provider.VisualProvider, 'generate', forbidden)
    monkeypatch.setattr(provider, 'json_request', forbidden)
    monkeypatch.setattr(codex_provider, 'generate', forbidden)


def mixed_wire():
    return wire(
        action(movement='Переместите деталь рукой с усилием 20 Н.'),
        action((3, 4), subject='Ткань', target='Пальцы', title='Переместите ткань',
               movement='Переместите ткань пальцами.'),
    )


def legacy_normalize(value, frames, selected=None):
    """Reproduce the previously valid v2 projection without changing the wire."""
    with patch.object(prose_evidence, 'unsupported_detail', return_value=None):
        content = grounding_v2.normalize_grounded_actions(value, frames, selected or session())
    return content


def assert_filtered(content, frames):
    assert len(content['steps']) == len(content['observations']) == 1
    assert content['observations'][0]['subject'] == 'Ткань'
    assert content['steps'][0]['evidence_frame_ids'] == [str(frame.id) for frame in frames[2:]]
    assert content['temporal_coverage']['pairs'][0]['change'] == 'unexplained_change'
    assert content['temporal_coverage']['pairs'][0]['observation_ids'] == []
    assert content['temporal_coverage']['pairs'][-1]['status'] == 'linked_action'
    assert any(gap['description'] == prose_evidence.REVIEW_REASON for gap in content['gaps'])
    assert all('20 Н' not in gap['description'] for gap in content['gaps'])


def test_fresh_mixed_cards_keep_the_supported_sibling_and_its_photos():
    frames, value = trusted_frames(images(4)), mixed_wire()
    original = deepcopy(value)
    content = grounding_v2.normalize_grounded_actions(value, frames, session())
    assert_filtered(content, frames)
    assert value == original
    assert grounding_v2.reconcile_grounding(content, frames, session()) == content


def test_saved_pre_guard_result_is_filtered_without_rewriting_or_whole_result_failure():
    frames = trusted_frames(images(4))
    content = legacy_normalize(mixed_wire(), frames)
    assert len(content['steps']) == 2
    original = deepcopy(content)
    once = grounding_v2.reconcile_grounding(content, frames, session())
    assert_filtered(once, frames)
    assert content == original
    assert grounding_v2.reconcile_grounding(once, frames, session()) == once
    assert once['frame_grounding']['batches'][0]['actions'] == original['frame_grounding']['batches'][0]['actions']
    assert len(once['frame_grounding']['batches'][0]['rejected_observation_ids']) == 1


def test_once_rejected_prose_card_cannot_return_when_the_rule_no_longer_matches():
    frames = trusted_frames(images(4))
    content = grounding_v2.reconcile_grounding(legacy_normalize(mixed_wire(), frames), frames, session())
    with patch.object(prose_evidence, 'unsupported_detail', return_value=None):
        checked = grounding_v2.reconcile_grounding(content, frames, session())
    assert len(checked['steps']) == 1 and checked['observations'][0]['subject'] == 'Ткань'
    assert checked['frame_grounding']['batches'][0]['rejected_observation_ids']


@pytest.mark.parametrize('corruption', ['raw_unknown_frame', 'observation_text', 'step_text',
                                       'coverage_link', 'coverage_description', 'gap_reference',
                                       'new_review_gap_reference'])
def test_new_text_filter_does_not_hide_corruption_in_a_card_it_would_remove(corruption):
    frames = trusted_frames(images(4))
    content = legacy_normalize(mixed_wire(), frames)
    if corruption == 'raw_unknown_frame':
        content['frame_grounding']['batches'][0]['actions'][0]['evidence'][1]['image_index'] = 99
    elif corruption == 'observation_text':
        content['observations'][0]['movement'] = 'Переместите деталь с усилием 99 Н.'
    elif corruption == 'step_text':
        content['steps'][0]['description'] += '\nИзменённый метод.'
    elif corruption == 'coverage_link':
        content['temporal_coverage']['pairs'][0]['observation_ids'] = []
    elif corruption == 'coverage_description':
        content['temporal_coverage']['pairs'][0]['description'] = 'Подменённое подтверждение.'
    elif corruption == 'gap_reference':
        content['gaps'][0]['evidence_frame_ids'] = [str(uuid.uuid4())]
    else:
        content['gaps'].append({
            'id': content['observations'][0]['id'] + ':grounding',
            'reason': 'unclear_action', 'description': prose_evidence.REVIEW_REASON,
            'evidence_frame_ids': [str(frame.id) for frame in frames[2:]],
            'start_ms': frames[2].coverage_start_ms, 'end_ms': frames[3].coverage_end_ms,
        })
    original = deepcopy(content)
    with pytest.raises(InvalidModelOutput):
        grounding_v2.reconcile_grounding(content, frames, session())
    assert content == original


def test_aggregate_rebuilds_each_window_without_losing_its_boundary_or_supported_step(monkeypatch):
    frames = trusted_frames(images(4))
    bad = action(movement='Переместите деталь рукой с усилием 20 Н.')
    good = action(subject='Ткань', target='Пальцы', title='Переместите ткань',
                  movement='Переместите ткань пальцами.')
    first = legacy_normalize(wire(bad), frames[:2])
    second = legacy_normalize(wire(good), frames[2:])
    windows = [SimpleNamespace(id=uuid.uuid4(), parent_id=None, state='completed', content=value)
               for value in (first, second)]
    monkeypatch.setattr(planning, 'analysis_data', lambda _: {'fixture': 'unchanged'})
    content = planning.aggregate_content(session(), windows)
    original = deepcopy(content)
    once = grounding_v2.reconcile_grounding(content, frames, session())
    assert len(once['steps']) == 1 and once['observations'][0]['subject'] == 'Ткань'
    assert [(pair['window_id'], pair['status']) for pair in once['temporal_coverage']['pairs']] == [
        (str(windows[0].id), 'unresolved'), (str(windows[1].id), 'linked_action')]
    assert once['temporal_coverage']['unresolved_pairs'] == 1
    assert once['analysis'] == {'fixture': 'unchanged'}
    assert len(once['frame_grounding']['batches']) == 2
    assert content == original and grounding_v2.reconcile_grounding(once, frames, session()) == once


def saved_legacy_mixed(team, draft, *, all_unsupported=False):
    create = VisualSession.objects.create
    with patch.object(VisualSession.objects, 'create', side_effect=lambda **kwargs: create(
            **kwargs, analysis_budget=deepcopy(session().analysis_budget))):
        selected, frames = saved_evidence(team, draft, count=4)
    value = mixed_wire()
    if all_unsupported:
        value['actions'][1]['movement'] = 'Переместите ткань с усилием 20 Н.'
    content = legacy_normalize(value, frames, selected)
    item = VisualProposal.objects.create(organization=team.org, session=selected, source_job_id=uuid.uuid4(),
                                         provider='local-test', model='no-model-call', content=content)
    return item, selected, frames


@pytest.mark.django_db
def test_read_twice_then_apply_keeps_only_supported_method_and_photos(team, draft):
    item, selected, frames = saved_legacy_mixed(team, draft)
    original = deepcopy(item.content)
    once, twice = services.proposal_data(item), services.proposal_data(item)
    assert once == twice and once['can_apply']
    assert_filtered(once, frames)
    assert {frame['id'] for frame in once['frames']} == {str(frame.id) for frame in frames}
    item.refresh_from_db()
    assert item.content == original and item.applied_at is None
    result = post(team.clients['author'], draft.url + f'/visual-proposals/{item.id}/apply',
                  {'confirm_replace': True}, etag=draft.working['etag'], key='prose-safe-apply')
    assert result.status_code == 200, result.json()
    applied = result.json()['steps']
    assert len(applied) == 1
    for field in ('title', 'description', 'start_ms', 'end_ms', 'evidence_frame_ids'):
        assert applied[0][field] == once['steps'][0][field]
    assert [frame['id'] for frame in applied[0]['evidence_frames']] == [str(frame.id) for frame in frames[2:]]
    item.refresh_from_db()
    assert item.content == original and item.applied_at is not None
    assert not AIUsageReservation.objects.exists()


@pytest.mark.django_db
def test_all_newly_unsupported_cards_become_review_gaps_and_cannot_erase_existing_steps(team, draft):
    item, selected, frames = saved_legacy_mixed(team, draft, all_unsupported=True)
    original = deepcopy(item.content)
    working = selected.instruction.working_copy
    working_revision = working.revision
    working_steps = list(working.steps.order_by('ordinal').values())
    preview = services.proposal_data(item)
    assert preview == services.proposal_data(item)
    assert preview['steps'] == preview['observations'] == [] and preview['can_apply'] is False
    assert len([gap for gap in preview['gaps'] if gap['description'] == prose_evidence.REVIEW_REASON]) == 2
    assert {frame['id'] for frame in preview['frames']} == {str(frame.id) for frame in frames}
    result = post(team.clients['author'], draft.url + f'/visual-proposals/{item.id}/apply',
                  {'confirm_replace': True}, etag=draft.working['etag'], key='prose-empty-apply')
    assert result.status_code == 422 and result.json()['error']['code'] == 'NO_PROPOSED_STEPS'
    item.refresh_from_db()
    working.refresh_from_db()
    assert item.content == original and item.applied_at is None
    assert working.revision == working_revision
    assert list(working.steps.order_by('ordinal').values()) == working_steps
    assert not AIUsageReservation.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize('corruption', ['observation_text', 'step_text', 'raw_index', 'coverage'])
def test_tampered_pre_guard_result_still_cannot_apply(team, draft, corruption):
    item, selected, _ = saved_legacy_mixed(team, draft)
    working = selected.instruction.working_copy
    working_revision = working.revision
    working_steps = list(working.steps.order_by('ordinal').values())
    content = deepcopy(item.content)
    if corruption == 'observation_text':
        content['observations'][0]['movement'] += ' Изменённый метод.'
    elif corruption == 'step_text':
        content['steps'][0]['description'] += ' Изменённый метод.'
    elif corruption == 'raw_index':
        content['frame_grounding']['batches'][0]['actions'][0]['evidence'][1]['image_index'] = 99
    else:
        content['temporal_coverage']['pairs'][0]['description'] += ' Подмена.'
    item = VisualProposal.objects.create(organization=team.org, session=selected, source_job_id=uuid.uuid4(),
                                         provider='local-test', model='no-model-call', content=content)
    original = deepcopy(item.content)
    assert services.proposal_data(item)['can_apply'] is False
    result = post(team.clients['author'], draft.url + f'/visual-proposals/{item.id}/apply',
                  {'confirm_replace': True}, etag=draft.working['etag'], key='prose-corrupt-apply')
    assert result.status_code == 422, result.json()
    item.refresh_from_db()
    assert item.content == original and item.applied_at is None
    working.refresh_from_db()
    assert working.revision == working_revision
    assert list(working.steps.order_by('ordinal').values()) == working_steps
    assert not AIUsageReservation.objects.exists()
