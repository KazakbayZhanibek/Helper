"""Structured evidence stays separate from authoring; no model calls are used."""
from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest

from ai_budget.models import AIUsageReservation
from core.models import PublishedVersion
from media_pipeline.models import ProcessingJob
from visual_drafts import planning, services
from visual_drafts.action_evidence import normalize
from visual_drafts.models import VisualProposal, VisualSession
from .conftest import post
from .test_visual_drafts import add_frames, create_session, make_proposal, session_url

pytestmark = pytest.mark.django_db


def content_v3(frames, namespace='batch'):
    refs = [str(frame.id) for frame in frames]
    left = min(frame.coverage_start_ms for frame in frames)
    right = max(frame.coverage_end_ms for frame in frames)
    observation = {'id': f'{namespace}:o1', 'kind': 'action', 'subject': 'деталь', 'title': 'Переместите деталь', 'before': None, 'before_frame_id': None, 'movement': 'Сдвиньте деталь вправо по поверхности.', 'action_frame_ids': refs, 'after': None, 'after_frame_id': None, 'evidence_frame_ids': refs, 'start_ms': left, 'end_ms': right}
    step = {'title': observation['title'], 'description': observation['movement'], 'evidence_frame_ids': refs, 'observation_ids': [observation['id']], 'start_ms': left, 'end_ms': right}
    return {'schema_version': '3', 'language': 'ru', 'steps': [step], 'observations': [observation], 'gaps': [], 'warnings': []}


def gap(frame, *, namespace='batch', reason='cut'):
    return {'id': f'{namespace}:g1', 'reason': reason, 'description': 'Начало действия не показано.', 'evidence_frame_ids': [str(frame.id)], 'start_ms': frame.coverage_start_ms, 'end_ms': frame.coverage_end_ms}


def state(frame, namespace='batch'):
    return {'id': f'{namespace}:o2', 'kind': 'state', 'subject': 'деталь', 'title': 'Деталь находится на поверхности', 'before': 'Деталь лежит на поверхности.', 'before_frame_id': str(frame.id), 'movement': None, 'action_frame_ids': [], 'after': None, 'after_frame_id': None, 'evidence_frame_ids': [str(frame.id)], 'start_ms': frame.coverage_start_ms, 'end_ms': frame.coverage_end_ms}


def window(content, *, parent=None, status='completed'):
    return SimpleNamespace(id=uuid.uuid4(), parent_id=parent, state=status, content=content)


@pytest.fixture
def aggregate(monkeypatch):
    monkeypatch.setattr(planning, 'analysis_data', lambda session: {'budget': {'calls_used': 0}})
    return lambda windows: planning.aggregate_content(SimpleNamespace(language='ru'), windows)


def test_selected_windows_keep_namespaced_actions_states_gaps_without_mutation(team, draft, aggregate):
    frames = add_frames(create_session(team, draft), count=4)
    first, second = content_v3(frames[:2], 'first'), content_v3(frames[2:], 'second')
    first['observations'].append(state(frames[0], 'first'))
    second['gaps'].append(gap(frames[-1], namespace='second'))
    before = deepcopy([first, second])
    result = aggregate([window(first), window(second)])
    assert result['schema_version'] == '3'
    assert [step['observation_ids'] for step in result['steps']] == [['first:o1'], ['second:o1']]
    assert {observation['id'] for observation in result['observations']} == {'first:o1', 'first:o2', 'second:o1'}
    assert result['gaps'] == second['gaps']
    result['steps'][0]['observation_ids'].append('mutation')
    result['observations'][0]['evidence_frame_ids'].clear()
    assert [first, second] == before


def test_identical_steps_merge_links_without_losing_observations(team, draft, aggregate):
    frames = add_frames(create_session(team, draft))
    first, second = content_v3(frames, 'first'), content_v3(frames, 'second')
    result = aggregate([window(first), window(second)])
    assert len(result['steps']) == 1
    assert result['steps'][0]['observation_ids'] == ['first:o1', 'second:o1']
    assert services.action_links_valid(result, {str(frame.id): frame for frame in frames})
    assert first['steps'][0]['observation_ids'] == ['first:o1']


@pytest.mark.parametrize('failure', ['pending', 'empty', 'uncertain'])
def test_unhelpful_refinement_retains_only_overview_metadata(team, draft, aggregate, failure):
    frames = add_frames(create_session(team, draft), count=4)
    original = content_v3(frames[:2], 'overview')
    original['gaps'] = [gap(frames[0], namespace='overview', reason='occluded')]
    parent = window(original)
    refined = content_v3(frames[2:], 'refinement')
    refined['gaps'] = [gap(frames[2], namespace='refinement')]
    if failure == 'empty':
        refined['steps'] = []
    if failure == 'uncertain':
        refined['gaps'][0]['reason'] = 'unclear_action'
    child = window(refined, parent=parent.id, status='pending' if failure == 'pending' else 'completed')
    result = aggregate([parent, child])
    assert result['observations'] == original['observations']
    assert result['gaps'] == original['gaps']
    assert result['steps'] == original['steps']
    assert 'refinement_unresolved' in {item['code'] for item in result['warnings']}


def test_successful_refinements_replace_all_overview_evidence(team, draft, aggregate):
    frames = add_frames(create_session(team, draft), count=6)
    original = content_v3(frames[:2], 'overview')
    original['gaps'] = [gap(frames[0], reason='occluded')]
    parent = window(original)
    left, right = content_v3(frames[2:4], 'left'), content_v3(frames[4:], 'right')
    left['observations'].append(state(frames[2], 'left'))
    right['gaps'] = [gap(frames[-1], namespace='right')]
    result = aggregate([parent, window(left, parent=parent.id), window(right, parent=parent.id)])
    assert {item['id'] for item in result['observations']} == {'left:o1', 'left:o2', 'right:o1'}
    assert result['gaps'] == right['gaps']
    assert 'refinement_unresolved' not in {item['code'] for item in result['warnings']}


def test_legacy_and_mixed_content_do_not_invent_action_provenance(team, draft, aggregate):
    _, proposal, frames = make_proposal(team, draft)
    legacy = deepcopy(proposal.content)
    result = aggregate([window(legacy)])
    assert result['schema_version'] == '2' and 'observations' not in result and 'gaps' not in result
    assert result['steps'] == legacy['steps'] and result['warnings'] == legacy['warnings']
    result = aggregate([window(legacy), window(content_v3(frames))])
    assert result['schema_version'] == '3'
    assert not services.action_links_valid(result, {str(frame.id): frame for frame in frames})
    # An unselected v3 child does not silently upgrade a legacy proposal.
    parent = window(legacy)
    result = aggregate([parent, window(content_v3(frames), parent=parent.id, status='pending')])
    assert result['schema_version'] == '2'


@pytest.mark.parametrize('protocol', [None, '2', '3'])
def test_empty_aggregate_preserves_pinned_protocol(monkeypatch, protocol):
    monkeypatch.setattr(planning, 'analysis_data', lambda session: {})
    session = SimpleNamespace(language='ru', analysis_budget={'visual_protocol': protocol} if protocol else {})
    result = planning.aggregate_content(session, [])
    assert result['schema_version'] == ('3' if protocol == '3' else '2')
    assert result['steps'] == []
    if protocol == '3':
        assert result['observations'] == [] and result['gaps'] == []
    else:
        assert 'observations' not in result and 'gaps' not in result


@pytest.mark.parametrize('reason,expected', [('occluded', True), ('sampling_gap', True), ('unclear_action', True), ('cut', False), ('outside_recording', False)])
@pytest.mark.parametrize('actions', [False, True])
def test_refinement_only_spends_calls_on_recoverable_gaps(reason, expected, actions):
    content = {'schema_version': '3', 'steps': [{}] if actions else [], 'gaps': [{'reason': reason}], 'warnings': [{'code': 'uncertain_action'}, {'code': 'insufficient_evidence'}]}
    assert planning.needs_refinement(content) is expected
    assert planning.needs_refinement({'schema_version': '2', 'steps': [], 'warnings': []})


def structured_proposal(team, draft):
    session = create_session(team, draft, mode='upload', state='completed')
    frames = add_frames(session, count=4)
    content = content_v3(frames[1:3])
    content['observations'][0].update(before='Деталь слева.', before_frame_id=str(frames[0].id), after='Деталь справа.', after_frame_id=str(frames[3].id), evidence_frame_ids=[str(frame.id) for frame in frames], start_ms=0, end_ms=8000)
    content['observations'].append(state(frames[0]))
    content['observations'][1]['evidence_frame_ids'] = [str(frame.id) for frame in frames[:2]]
    content['gaps'] = [gap(frames[3])]
    content['gaps'][0]['evidence_frame_ids'] = [str(frame.id) for frame in frames[2:]]
    wire = deepcopy(content)
    wire.pop('steps')
    for observation in wire['observations']:
        observation['id'] = observation['id'].split(':')[-1]
        observation.pop('start_ms')
        observation.pop('end_ms')
    for item in wire['gaps']:
        for field in ('id', 'start_ms', 'end_ms'):
            item.pop(field)
    content = normalize(wire, frames, session)
    proposal = VisualProposal.objects.create(organization=team.org, session=session, source_job_id=uuid.uuid4(), provider='test', model='fixture', content=content)
    return session, proposal, frames


def apply(team, draft, proposal):
    return post(team.clients['author'], draft.url + f'/visual-proposals/{proposal.id}/apply', {'confirm_replace': True}, etag=draft.working['etag'], key='structured-apply')


def test_gaps_and_states_only_render_scoped_frames_without_becoming_steps(team, draft):
    session, proposal, frames = structured_proposal(team, draft)
    foreign = add_frames(create_session(team, draft))[0]
    proposal.content['steps'] = []
    proposal.content['observations'] = [state(frames[0])]
    proposal.content['gaps'] = [gap(frames[3]), gap(foreign, namespace='foreign')]
    data = services.proposal_data(proposal)
    assert {frame['id'] for frame in data['frames']} == {str(frames[0].id), str(frames[3].id)}
    assert not data['can_apply']
    assert data['gaps'] == proposal.content['gaps']


def test_v3_apply_projects_only_actions_and_keeps_full_evidence_history(team, draft):
    session, proposal, frames = structured_proposal(team, draft)
    original = deepcopy(proposal.content)
    data = services.proposal_data(proposal)
    assert data['can_apply'] and len(data['frames']) == 4
    response = apply(team, draft, proposal)
    assert response.status_code == 200, response.json()
    assert len(response.json()['steps']) == 1
    step = session.instruction.working_copy.steps.get()
    assert step.title == original['steps'][0]['title']
    assert step.description == data['steps'][0]['description'] == original['observations'][0]['movement']
    assert step.evidence_frame_ids == [str(frame.id) for frame in frames[1:3]]
    assert step.evidence_visual_session_id == session.id and step.origin == 'ai'
    proposal.refresh_from_db()
    assert proposal.content == original and proposal.applied_at is not None
    assert not PublishedVersion.objects.exists() and not AIUsageReservation.objects.exists()


@pytest.mark.parametrize('corruption', ['state_link', 'missing_link', 'legacy_step', 'text', 'time', 'before', 'after', 'gap', 'observation', 'missing_action_step'])
def test_v3_apply_rejects_broken_action_or_metadata_evidence_atomically(team, draft, corruption):
    session, proposal, _ = structured_proposal(team, draft)
    content = proposal.content
    if corruption == 'state_link':
        content['steps'][0]['observation_ids'] = [content['observations'][1]['id']]
    elif corruption == 'missing_link':
        content['steps'][0]['observation_ids'] = ['unknown:o1']
    elif corruption == 'legacy_step':
        content['steps'][0].pop('observation_ids')
    elif corruption == 'text':
        content['steps'][0]['description'] = 'Добавленное неподтверждённое действие.'
    elif corruption == 'time':
        content['steps'][0]['start_ms'] = 1
    elif corruption in {'before', 'after'}:
        content['observations'][0][f'{corruption}_frame_id'] = str(uuid.uuid4())
    elif corruption == 'gap':
        content['gaps'][0]['evidence_frame_ids'] = [str(uuid.uuid4())]
    elif corruption == 'observation':
        content['observations'][1]['evidence_frame_ids'] = [str(uuid.uuid4())]
    else:
        unsupported = deepcopy(content['observations'][0])
        unsupported['id'] = 'other:o1'
        content['observations'].append(unsupported)
    # Proposal JSON is immutable even in the test DB. Create the deliberately
    # invalid historical record at insertion, never disable that guard.
    proposal = VisualProposal.objects.create(organization=team.org, session=session, source_job_id=uuid.uuid4(), provider='test', model='fixture', content=content)
    before = list(session.instruction.working_copy.steps.values())
    assert not services.proposal_data(proposal)['can_apply']
    response = apply(team, draft, proposal)
    assert response.status_code == 422 and response.json()['error']['code'] == 'VISUAL_EVIDENCE_INVALID'
    assert list(session.instruction.working_copy.steps.values()) == before
    proposal.refresh_from_db()
    assert proposal.applied_at is None


def test_v2_proposal_still_applies(team, draft):
    _, proposal, _ = make_proposal(team, draft)
    assert services.proposal_data(proposal)['can_apply']
    response = apply(team, draft, proposal)
    assert response.status_code == 200, response.json()


@pytest.fixture
def local_config(settings):
    settings.STEP_DRAFT_BACKEND = 'openai'
    settings.STEP_DRAFT_ENDPOINT = 'http://127.0.0.1:12345/v1'
    settings.STEP_DRAFT_MODEL = 'gpt-5.4-mini'
    settings.STEP_DRAFT_API_KEY = ''
    settings.EXTERNAL_AI_ENABLED = False


@pytest.mark.parametrize('enabled,expected', [(True, '3'), (False, '2')])
def test_live_protocol_is_pinned_when_session_starts(team, draft, settings, local_config, enabled, expected):
    settings.VISUAL_ACTION_EVIDENCE = enabled
    response = post(team.clients['author'], draft.url + '/visual-sessions', {'consent_images': True}, etag=draft.working['etag'], key='pin-start')
    assert response.status_code == 201, response.json()
    session = VisualSession.objects.get(pk=response.json()['id'])
    assert session.analysis_budget == ({'visual_protocol': '3'} if enabled else {})
    add_frames(session)
    settings.VISUAL_ACTION_EVIDENCE = not enabled
    response = post(team.clients['author'], session_url(team, session) + '/analyze', {}, key='pin-analysis')
    assert response.status_code == 202, response.json()
    job = ProcessingJob.objects.get(pk=response.json()['id'])
    assert job.payload['visual_protocol'] == expected
    assert not AIUsageReservation.objects.exists()


@pytest.mark.parametrize('enabled,expected', [(True, '3'), (False, '2')])
def test_uploaded_protocol_and_budget_are_fixed_in_session_and_job(team, draft, settings, local_config, enabled, expected):
    settings.VISUAL_ACTION_EVIDENCE = enabled
    response = post(team.clients['author'], draft.url + '/visual-proposals', {'consent_images': True, 'max_ai_calls': 2, 'max_cost_microusd': 500000}, etag=draft.working['etag'], key='pin-upload')
    assert response.status_code == 202, response.json()
    job = ProcessingJob.objects.get(pk=response.json()['id'])
    session = VisualSession.objects.get(pk=job.payload['visual_session_id'])
    expected_budget = {'max_calls': 2, 'max_cost_microusd': 500000}
    if enabled:
        expected_budget['visual_protocol'] = '3'
    assert session.analysis_budget == expected_budget and job.payload['visual_protocol'] == expected
    settings.VISUAL_ACTION_EVIDENCE = not enabled
    session.refresh_from_db()
    job.refresh_from_db()
    assert session.analysis_budget == expected_budget and job.payload['visual_protocol'] == expected
    assert not AIUsageReservation.objects.exists()
