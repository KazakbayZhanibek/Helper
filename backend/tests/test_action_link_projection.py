"""Stored proposals are projected consistently for reading and applying, without AI."""
from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest
from django.utils import timezone

from ai_budget.models import AIUsageReservation
from ai_drafts.validation import InvalidModelOutput
from core import services as core
from core.models import WorkingCopy
from visual_drafts import action_evidence, services, short_actions
from visual_drafts.models import VisualProposal
from .conftest import post
from .test_instruction_photos import evidence
from .test_action_protocol import action_value

pytestmark = pytest.mark.django_db


def legacy_content(frames, session):
    refs = [{'id': str(frame.id)} for frame in frames]
    raw = action_value(refs)
    raw['observations'].append({**deepcopy(raw['observations'][0]), 'id': 'o2',
                               'title': 'Поднимите инструмент',
                               'movement': 'Поднимите инструмент вверх.',
                               'action_frame_ids': [item['id'] for item in refs[:2]],
                               'evidence_frame_ids': [item['id'] for item in refs[:2]]})
    content = action_evidence.normalize(raw, frames, session)
    first, second = [item['id'] for item in content['observations']]
    content['temporal_coverage'] = {'version': 'v1', 'unresolved_pairs': 1, 'pairs': [
        {'pair_index': index, 'from_frame_id': str(left.id), 'to_frame_id': str(right.id),
         'gap_ms': right.timestamp_ms - left.timestamp_ms, 'change': 'visible_action',
         'description': 'Рука перемещает предмет.', 'observation_ids': [first if index == 0 else second],
         'status': 'linked_action' if index == 0 else 'unresolved'}
        for index, (left, right) in enumerate(zip(frames, frames[1:]))]}
    return content


@pytest.fixture
def legacy(team, draft):
    session, frames = evidence(team, draft)
    content = legacy_content(frames, session)
    item = VisualProposal.objects.create(organization=team.org, session=session, source_job_id=uuid.uuid4(),
                                         provider='local-test', model='no-model-call', content=content)
    return SimpleNamespace(session=session, frames=frames, item=item, content=content,
                           url=draft.url + f'/visual-proposals/{item.id}', draft=draft)


def distinct_candidate(legacy, content):
    """Even corrupt test fixtures must not rewrite an immutable saved answer."""
    legacy.item = VisualProposal.objects.create(
        organization=legacy.item.organization, session=legacy.session, source_job_id=uuid.uuid4(),
        provider='local-test', model='no-model-call', content=content)
    legacy.url = legacy.draft.url + f'/visual-proposals/{legacy.item.id}'


def test_read_projects_unapplied_proposal_without_mutating_history_or_working_copy(team, legacy):
    original = deepcopy(legacy.item.content)
    updated = legacy.item.updated_at
    working_before = core.working_data(WorkingCopy.objects.get(pk=legacy.draft.working['id']))
    first = team.clients['author'].get(legacy.url)
    second = team.clients['author'].get(legacy.url)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    result = first.json()
    assert result['can_apply'] and len(result['steps']) == 1
    assert result['steps'][0] == {**original['steps'][0], 'description': original['observations'][0]['movement']}
    assert len(result['observations']) == 1 and result['gaps']
    assert all('Поднимите инструмент' not in gap['description'] for gap in result['gaps'])
    legacy.item.refresh_from_db()
    assert legacy.item.content == original and legacy.item.updated_at == updated
    assert core.working_data(WorkingCopy.objects.get(pk=legacy.draft.working['id'])) == working_before
    assert AIUsageReservation.objects.count() == 0


@pytest.mark.parametrize('mode', ['replace', 'append'])
def test_apply_uses_the_same_projected_steps_and_keeps_raw_proposal(team, legacy, mode):
    projected = team.clients['author'].get(legacy.url).json()
    result = post(team.clients['author'], legacy.url + '/apply',
                  {'confirm_replace': True, 'mode': mode}, etag=legacy.draft.working['etag'], key=f'project-{mode}')
    assert result.status_code == 200, result.json()
    expected_count = 1 + (len(legacy.draft.working['steps']) if mode == 'append' else 0)
    assert len(result.json()['steps']) == expected_count
    applied = result.json()['steps'][-1]
    for field in ('title', 'description', 'start_ms', 'end_ms', 'evidence_frame_ids'):
        assert applied[field] == projected['steps'][0][field]
    assert 'uncertain_action' in applied['warning_codes']
    legacy.item.refresh_from_db()
    assert legacy.item.content == legacy.content and legacy.item.applied_at is not None
    assert AIUsageReservation.objects.count() == 0


def test_empty_projection_cannot_replace_existing_work(team, legacy):
    content = deepcopy(legacy.content)
    for pair in content['temporal_coverage']['pairs']:
        pair.update(change='no_clear_change', observation_ids=[], status='no_clear_change')
    distinct_candidate(legacy, content)
    result = team.clients['author'].get(legacy.url)
    assert result.status_code == 200
    assert result.json()['steps'] == [] and not result.json()['can_apply']
    applied = post(team.clients['author'], legacy.url + '/apply', {'confirm_replace': True},
                   etag=legacy.draft.working['etag'], key='empty-project')
    assert applied.status_code == 422 and applied.json()['error']['code'] == 'NO_PROPOSED_STEPS'
    assert core.working_data(WorkingCopy.objects.get(pk=legacy.draft.working['id'])) == legacy.draft.working
    legacy.item.refresh_from_db()
    assert legacy.item.applied_at is None and legacy.item.content == content


def test_already_applied_history_is_not_rewritten_by_projection(team, legacy):
    legacy.item.applied_at = timezone.now()
    legacy.item.applied_by = team.users['author']
    legacy.item.save(update_fields=['applied_at', 'applied_by'])
    result = team.clients['author'].get(legacy.url).json()
    assert result['steps'] == legacy.content['steps']
    assert result['observations'] == legacy.content['observations']
    assert not result['can_apply']


@pytest.mark.parametrize('corruption', ['step_text', 'foreign_action_ref', 'foreign_pair_ref', 'pair_time', 'cut', 'gap_cut', 'unknown_link'])
def test_corrupt_original_never_becomes_applicable_after_filtering(team, legacy, corruption):
    value = deepcopy(legacy.content)
    if corruption == 'step_text': value['steps'][1]['description'] = 'Непроверенная подмена.'
    elif corruption == 'foreign_action_ref': value['observations'][1]['action_frame_ids'][0] = str(uuid.uuid4())
    elif corruption == 'foreign_pair_ref': value['temporal_coverage']['pairs'][1]['to_frame_id'] = str(uuid.uuid4())
    elif corruption == 'pair_time': value['temporal_coverage']['pairs'][1]['gap_ms'] += 1
    elif corruption == 'cut': value['temporal_coverage']['pairs'][0].update(change='cut', observation_ids=[])
    elif corruption == 'gap_cut': value['gaps'] = [{'id': 'cut:gap', 'reason': 'cut', 'description': 'Монтаж.',
                                                  'evidence_frame_ids': [str(frame.id) for frame in legacy.frames[:2]],
                                                  'start_ms': 0, 'end_ms': 2000}]
    elif corruption == 'unknown_link': value['temporal_coverage']['pairs'][1]['observation_ids'] = ['unknown:action']
    distinct_candidate(legacy, value)
    shown = team.clients['author'].get(legacy.url)
    assert shown.status_code == 200 and not shown.json()['can_apply']
    applied = post(team.clients['author'], legacy.url + '/apply', {'confirm_replace': True},
                   etag=legacy.draft.working['etag'], key=corruption)
    assert applied.status_code == 422 and applied.json()['error']['code'] == 'VISUAL_EVIDENCE_INVALID'
    assert core.working_data(WorkingCopy.objects.get(pk=legacy.draft.working['id'])) == legacy.draft.working


@pytest.mark.parametrize('change', ['visible_action', 'unexplained_change', 'no_clear_change'])
def test_projection_is_idempotent_including_orphan_no_change_pairs(legacy, change):
    content = deepcopy(legacy.content)
    for pair in content['temporal_coverage']['pairs']:
        pair.update(change=change, observation_ids=[], status=change)
    original = deepcopy(content)
    projected = short_actions.reconcile_action_links(content, legacy.frames, legacy.session)
    assert projected['steps'] == [] and projected['temporal_coverage']['unresolved_pairs'] == 2
    again = short_actions.reconcile_action_links(projected, legacy.frames, legacy.session)
    assert again == projected and content == original


def test_aggregate_step_keeps_supported_identity_and_all_its_shared_frames(legacy):
    content = deepcopy(legacy.content)
    first = content['observations'][0]
    duplicate = {**deepcopy(first), 'id': 'other-batch:o1'}
    content['observations'] = [first, duplicate]
    content['steps'] = [{**content['steps'][0], 'observation_ids': [first['id'], duplicate['id']]}]
    for pair in content['temporal_coverage']['pairs']:
        pair.update(observation_ids=[first['id']], status='linked_action')
    projected = short_actions.reconcile_action_links(content, legacy.frames, legacy.session)
    assert projected['steps'] == [legacy.content['steps'][0]]
    assert projected['observations'] == [first]
    assert projected['gaps'] == []
    assert services.action_links_valid(projected, {str(frame.id): frame for frame in legacy.frames})
    assert short_actions.reconcile_action_links(projected, legacy.frames, legacy.session) == projected


def test_legacy_v3_without_temporal_inventory_is_unchanged(legacy):
    content = deepcopy(legacy.content)
    content.pop('temporal_coverage')
    assert short_actions.reconcile_action_links(content, legacy.frames, legacy.session) == content


def test_multiwindow_inventory_uses_namespaced_links_not_local_pair_indices(legacy):
    content = deepcopy(legacy.content)
    for row in content['temporal_coverage']['pairs']:
        row['window_id'] = 'window-a'
    # A separate window can restart pair_index, and its same local o1 suffix
    # must not support another window's unsupported action identity.
    content['temporal_coverage']['pairs'].append({
        **deepcopy(content['temporal_coverage']['pairs'][0]),
        'window_id': 'window-b', 'pair_index': 0,
    })
    projected = short_actions.reconcile_action_links(content, legacy.frames, legacy.session)
    assert len(projected['steps']) == 1
    assert len(projected['temporal_coverage']['pairs']) == 3
    assert projected['temporal_coverage']['unresolved_pairs'] == 1


def test_old_derived_question_does_not_repeat_rejected_instruction(legacy):
    content = deepcopy(legacy.content)
    left, right = legacy.frames[1:]
    content['gaps'] = [{'id': 'old:question', 'reason': 'unclear_action',
                        'description': 'Уточните короткий переход по записи: Поднимите инструмент вверх.',
                        'evidence_frame_ids': [str(left.id), str(right.id)],
                        'start_ms': left.coverage_start_ms, 'end_ms': right.coverage_end_ms}]
    original = deepcopy(content)
    projected = short_actions.reconcile_action_links(content, legacy.frames, legacy.session)
    assert projected['gaps'][0]['description'] == short_actions.ACTION_LINK_REVIEW_REASON
    assert short_actions.reconcile_action_links(projected, legacy.frames, legacy.session) == projected
    assert content == original
