"""Pinned same-call grounding integration; all inference is a local fixture."""
from copy import deepcopy
from itertools import product
import json
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from ai_budget.models import AIUsageReservation
from ai_drafts import codex_provider
from ai_drafts.providers import ProviderFailure
from ai_drafts.validation import InvalidModelOutput
from media_pipeline.exceptions import ProcessingFailure
from media_pipeline.worker import JobContext, process_one
from visual_drafts import planning, provider, services, worker
from visual_drafts.models import VisualAnalysisWindow, VisualProposal, VisualSession
from .conftest import post
from .test_action_protocol import images, instance
from .test_frame_grounding import wire as fact_wire
from .test_instruction_photos import evidence as saved_evidence
from .test_short_action_coverage import wire as legacy_wire, trusted_frames, session
from .test_visual_full_video import full_video, queue, fake_extraction


def pinned_session(**changes):
    selected = session()
    selected.analysis_budget = {'visual_protocol': '3', 'short_action_coverage': 'v1', 'frame_grounding': 'v1'}
    selected.analysis_budget.update(changes)
    return selected


def grounded_provider(backend='codex'):
    selected = instance(backend)
    selected.short_action_coverage = 'v1'
    selected.frame_grounding = 'v1'
    return selected


def grounded_wire(source, *, unclear=False):
    value = fact_wire(source)
    if unclear:
        for state in value['frame_states']:
            if state['entity_id'] == 'e1':
                state.update(visibility='unclear', relations=[])
    return value


@pytest.mark.parametrize('job_pin,session_pin', [(None, 'v1'), ('v1', None), ('v3', 'v3'), ('v2', 'v1')])
def test_changed_or_missing_grounding_pin_is_rejected(job_pin, session_pin):
    selected = pinned_session(frame_grounding=session_pin)
    payload = {**selected.analysis_budget, 'frame_grounding': job_pin}
    with pytest.raises(ProcessingFailure) as failure:
        worker.protocol_for_job(SimpleNamespace(payload=payload), selected)
    assert failure.value.code == 'VISUAL_PROTOCOL_INVALID'


@pytest.mark.django_db
@pytest.mark.parametrize('actions,coverage,grounding', list(product((False, True), repeat=3)))
def test_capabilities_report_only_the_effective_grounded_mode_as_no_refinement(team, settings, actions, coverage, grounding):
    settings.VISUAL_ACTION_EVIDENCE = actions
    settings.VISUAL_SHORT_ACTION_COVERAGE = coverage
    settings.VISUAL_FRAME_GROUNDING = grounding
    result = provider.capabilities(team.org)
    assert result['automatic_refinements'] is not (actions and coverage and grounding)
    assert result['limits']['max_analysis_frames'] == 12
    assert result['limits']['max_analysis_calls'] == 60


@pytest.mark.parametrize('changes', [
    {'visual_protocol': '2'}, {'short_action_coverage': None}, {'semantic_review': 'v1'},
])
def test_grounding_requires_covered_v3_without_second_review(changes):
    selected = pinned_session(**changes)
    with pytest.raises(ProcessingFailure) as failure:
        worker.protocol_for_job(SimpleNamespace(payload=deepcopy(selected.analysis_budget)), selected)
    assert failure.value.code == 'VISUAL_PROTOCOL_INVALID'


def test_grounding_upload_only_and_legacy_pins_remain_valid():
    selected = pinned_session()
    payload = deepcopy(selected.analysis_budget)
    selected.mode = 'live'
    with pytest.raises(ProcessingFailure):
        worker.protocol_for_job(SimpleNamespace(payload=payload), selected)
    selected.mode = 'upload'
    del selected.analysis_budget['frame_grounding']
    del payload['frame_grounding']
    assert worker.protocol_for_job(SimpleNamespace(payload=payload), selected) == '3'


@pytest.mark.parametrize('grounding', [None, {'version': 'v2', 'batches': []}, {'version': 'v1'},
                                      {'version': 'v1', 'batches': []}, []])
def test_completed_window_cannot_downgrade_or_lose_fact_batch(grounding):
    selected = pinned_session()
    content = {'schema_version': '3', 'temporal_coverage': {'version': 'v1'}}
    if grounding is not None:
        content['frame_grounding'] = grounding
    checkpoint = SimpleNamespace(state='completed', content=content)
    with pytest.raises(ProcessingFailure) as failure:
        worker.run_window(SimpleNamespace(payload=deepcopy(selected.analysis_budget)), selected, checkpoint, None, None)
    assert failure.value.code == 'VISUAL_PROTOCOL_INVALID'


def test_completed_legacy_window_cannot_acquire_grounding():
    selected = pinned_session()
    del selected.analysis_budget['frame_grounding']
    checkpoint = SimpleNamespace(state='completed', content={
        'schema_version': '3', 'temporal_coverage': {'version': 'v1'},
        'frame_grounding': {'version': 'v1', 'batches': []},
    })
    with pytest.raises(ProcessingFailure):
        worker.run_window(SimpleNamespace(payload=deepcopy(selected.analysis_budget)), selected, checkpoint, None, None)


@pytest.mark.parametrize('mode,protocol,coverage,pin', [
    ('live', '3', 'v1', 'v1'), ('upload', '2', None, 'v1'),
    ('upload', '3', None, 'v1'), ('upload', '3', 'v1', 'v3'),
])
def test_request_contract_rejects_illegal_combination(mode, protocol, coverage, pin):
    with pytest.raises(ProviderFailure, match='VISUAL_PROTOCOL_INVALID'):
        provider.request_contract(protocol, 'ru', mode, images(2),
                                  short_action_coverage=coverage, frame_grounding=pin)


def test_old_wire_is_not_accepted_as_grounded_response():
    source = images(2)
    with pytest.raises(InvalidModelOutput):
        provider.validate(legacy_wire(source), trusted_frames(source), pinned_session(),
                          expected_version='3', expected_coverage='v1', expected_grounding='v1')


@pytest.mark.parametrize('backend', ['codex', 'openai'])
def test_grounding_contract_keeps_existing_input_and_image_caps(backend):
    source = images(12)
    system, schema, request, body = grounded_provider(backend).prepare_request(
        language='mixed', mode='upload', frames=source)
    assert schema['properties']['schema_version']['enum'] == ['3-grounded-actions-v1']
    assert request['frame_grounding'] == 'v1'
    assert len(request['frames']) == 12 and len(request['temporal_pairs']) == 11
    assert len(json.dumps({'system': system, 'schema': schema, 'request': request}, ensure_ascii=False).encode()) + 1024 <= 32768
    if body:
        assert body['max_completion_tokens'] == 8192
        assert len([item for item in body['messages'][1]['content'] if item['type'] == 'image_url']) == 12


def test_grounded_unknowns_never_trigger_optional_refinement():
    assert not planning.needs_refinement({'schema_version': '3', 'steps': [],
        'frame_grounding': {'version': 'v1', 'batches': []},
        'gaps': [{'reason': 'unclear_action'}], 'warnings': [{'code': 'uncertain_action'}]})
    assert planning.needs_refinement({'schema_version': '3', 'steps': [],
        'gaps': [{'reason': 'unclear_action'}], 'warnings': [{'code': 'uncertain_action'}]})


def test_one_request_per_transport_keeps_same_contract_images_and_output_caps(monkeypatch):
    source = images(12)
    answer, captured = grounded_wire(source), {}

    def http(url, body, **kwargs):
        assert 'http' not in captured
        captured['http'] = deepcopy(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}]}

    def cli(**kwargs):
        assert 'cli' not in captured
        captured['cli'] = kwargs
        return answer

    monkeypatch.setattr(provider, 'json_request', http)
    monkeypatch.setattr(provider, 'provider_api_key', lambda *_: '')
    monkeypatch.setattr(codex_provider, 'generate', cli)
    for backend in ('openai', 'codex'):
        assert grounded_provider(backend).generate(language='ru', mode='upload', frames=source) == answer
    body, cli_request = captured['http'], captured['cli']
    assert body['messages'][0]['content'] == cli_request['system']
    assert body['response_format']['json_schema']['schema'] == cli_request['schema']
    assert body['max_completion_tokens'] == 8192
    assert cli_request['max_output_bytes'] == 32768 and cli_request['reasoning_effort'] == 'medium'
    assert cli_request['frames'] is source
    assert Draft202012Validator(cli_request['schema'], format_checker=FormatChecker()).is_valid(answer)
    output = provider.validate(answer, trusted_frames(source), pinned_session(),
                               expected_version='3', expected_coverage='v1', expected_grounding='v1')
    assert output['frame_grounding']['version'] == 'v1' and len(output['steps']) == 1


@pytest.mark.django_db
def test_worker_keeps_three_overview_calls_and_never_refines_new_unknowns(team, full_video, settings, monkeypatch):
    settings.VISUAL_ACTION_EVIDENCE = settings.VISUAL_SHORT_ACTION_COVERAGE = settings.VISUAL_FRAME_GROUNDING = True
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
    monkeypatch.setattr(JobContext, 'call', lambda self, function, timeout: function())
    calls = []

    class LocalModel(provider.VisualProvider):
        def __init__(self, organization):
            self.provider, self.model, self.endpoint = 'openai', 'gpt-5.4-mini', 'https://api.openai.com/v1'

        def generate(self, *, language, mode, frames, context=None):
            assert self.protocol == '3' and self.short_action_coverage == self.frame_grounding == 'v1'
            calls.append(frames[0]['timestamp_ms'])
            self._usage_recorder({'prompt_tokens': 1000, 'completion_tokens': 100}, f'grounded-fixture-{len(calls)}')
            return grounded_wire(frames, unclear=True)

    monkeypatch.setattr(worker, 'VisualProvider', LocalModel)
    job, selected = queue(team, full_video, max_ai_calls=6)
    assert selected.analysis_budget['frame_grounding'] == job.payload['frame_grounding'] == 'v1'
    settings.VISUAL_FRAME_GROUNDING = False  # Existing session keeps its pin.
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done', (job.error_code, job.error_summary)
    assert calls == [0, 30000, 60000]
    assert AIUsageReservation.objects.count() == AIUsageReservation.objects.filter(state='settled').count() == 3
    assert not selected.analysis_windows.filter(parent__isnull=False).exists()
    content = VisualProposal.objects.get(source_job_id=job.id).content
    assert content['steps'] == [] and content['gaps']
    assert len(content['frame_grounding']['batches']) == 3
    assert all(batch['rejected_observation_ids'] for batch in content['frame_grounding']['batches'])
    assert content['analysis']['budget']['calls_used'] == 3 and content['analysis']['budget']['max_calls'] == 6
    assert 'refinement_incomplete' not in {item['code'] for item in content['warnings']}
    assert not full_video.working.steps.exists()


def normalized_empty(batch_id='b1'):
    return {'schema_version': '3', 'language': 'ru', 'step_format': 'teaching_v1',
            'steps': [], 'observations': [], 'gaps': [], 'warnings': [],
            'frame_grounding': {'version': 'v1', 'batches': [
                {'id': batch_id, 'entities': [{'id': 'e1', 'label': 'Предмет'}],
                 'states': [{'id': 's1', 'visibility': 'unclear'}], 'observation_links': []}]}}


def test_aggregate_keeps_rejected_or_unknown_fact_batches_without_steps(monkeypatch):
    monkeypatch.setattr(planning, 'analysis_data', lambda _: {})
    first, second = normalized_empty('b1'), normalized_empty('b2')
    original = deepcopy([first, second])
    windows = [SimpleNamespace(id=uuid.uuid4(), parent_id=None, state='completed', content=value)
               for value in (first, second)]
    result = planning.aggregate_content(pinned_session(), windows)
    assert result['steps'] == []
    assert result['frame_grounding']['batches'] == [first['frame_grounding']['batches'][0], second['frame_grounding']['batches'][0]]
    result['frame_grounding']['batches'][0]['states'][0]['visibility'] = 'mutated'
    assert [first, second] == original
    assert 'refinement_incomplete' not in {w['code'] for w in result['warnings']}


@pytest.mark.parametrize('corrupt', ['missing_pin', 'wrong_version', 'missing_batches', 'conflicting_batch'])
def test_aggregate_refuses_missing_or_conflicting_grounding(monkeypatch, corrupt):
    monkeypatch.setattr(planning, 'analysis_data', lambda _: {})
    first, second = normalized_empty(), normalized_empty('b2')
    if corrupt == 'missing_pin':
        second.pop('frame_grounding')
    elif corrupt == 'wrong_version':
        second['frame_grounding']['version'] = 'v2'
    elif corrupt == 'missing_batches':
        second['frame_grounding'].pop('batches')
    else:
        second['frame_grounding']['batches'][0]['id'] = 'b1'
        second['frame_grounding']['batches'][0]['states'] = []
    windows = [SimpleNamespace(id=uuid.uuid4(), parent_id=None, state='completed', content=value)
               for value in (first, second)]
    with pytest.raises(ProcessingFailure) as failure:
        planning.aggregate_content(pinned_session(), windows)
    assert failure.value.code == 'VISUAL_PROTOCOL_INVALID'


def test_aggregate_empty_failed_analysis_retains_pinned_mode(monkeypatch):
    monkeypatch.setattr(planning, 'analysis_data', lambda _: {})
    window = SimpleNamespace(id=uuid.uuid4(), parent_id=None, state='failed')
    content = planning.aggregate_content(pinned_session(), [window])
    assert content['steps'] == []
    assert content['frame_grounding'] == {'version': 'v1', 'batches': []}


def test_complete_children_replace_parent_without_losing_rejected_fact_batches(monkeypatch):
    monkeypatch.setattr(planning, 'analysis_data', lambda _: {})
    refs = [str(uuid.uuid4()) for _ in range(4)]
    fingerprints = {ref: (index * 5000, str(index) * 64) for index, ref in enumerate(refs)}
    monkeypatch.setattr(planning, '_frame_fingerprints', lambda *_: fingerprints)
    parent = SimpleNamespace(id=uuid.uuid4(), parent_id=None, state='completed',
                             start_ms=0, end_ms=20000, frame_ids=[refs[0], refs[3]], content=normalized_empty('parent'))
    children = []
    for index in range(2):
        content = normalized_empty(f'child{index}')
        pair_refs = refs[index * 2:index * 2 + 2]
        content['temporal_coverage'] = {'version': 'v1', 'unresolved_pairs': 1, 'pairs': [{
            'pair_index': 0, 'from_frame_id': pair_refs[0], 'to_frame_id': pair_refs[1], 'gap_ms': 5000,
            'change': 'unexplained_change', 'status': 'unresolved', 'observation_ids': [],
        }]}
        children.append(SimpleNamespace(id=uuid.uuid4(), parent_id=parent.id, state='completed',
                                        start_ms=index * 10000, end_ms=(index + 1) * 10000,
                                        frame_ids=pair_refs, content=content))
    result = planning.aggregate_content(pinned_session(), [parent, *children])
    assert [batch['id'] for batch in result['frame_grounding']['batches']] == ['child0', 'child1']
    assert result['steps'] == []
    assert result['temporal_coverage']['unresolved_pairs'] == 2


@pytest.mark.django_db
def test_grounded_input_overflow_stops_without_reserving_or_dispatching(team, full_video, settings, monkeypatch):
    from visual_drafts import grounding
    settings.VISUAL_ACTION_EVIDENCE = settings.VISUAL_SHORT_ACTION_COVERAGE = settings.VISUAL_FRAME_GROUNDING = True
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
    monkeypatch.setattr(grounding, 'SYSTEM', 'я' * 32768)

    class NoTransport(provider.VisualProvider):
        def __init__(self, organization):
            self.provider, self.model, self.endpoint = 'openai', 'gpt-5.4-mini', 'https://api.openai.com/v1'

        def generate(self, **kwargs):
            pytest.fail('Rejected input reached transport')

    monkeypatch.setattr(worker, 'VisualProvider', NoTransport)
    job, selected = queue(team, full_video, max_ai_calls=6)
    assert selected.analysis_budget['frame_grounding'] == job.payload['frame_grounding'] == 'v1'
    assert process_one()
    assert not AIUsageReservation.objects.exists()
    assert not selected.analysis_windows.filter(parent__isnull=False).exists()
    content = VisualProposal.objects.get(source_job_id=job.id).content
    assert content['steps'] == [] and content['frame_grounding'] == {'version': 'v1', 'batches': []}


def saved_grounded_proposal(team, draft, *, rejected=False):
    # The shared photo fixture predates contract pins. Inject the pin at initial
    # creation, keeping the real database immutability guard fully active.
    create = VisualSession.objects.create
    def create_pinned(**kwargs):
        return create(**kwargs, analysis_budget=deepcopy(pinned_session().analysis_budget))
    with patch.object(VisualSession.objects, 'create', side_effect=create_pinned):
        selected, actual_frames = saved_evidence(team, draft, count=3)
    source = [{'id': str(frame.id), 'sequence': frame.sequence, 'timestamp_ms': frame.timestamp_ms}
              for frame in actual_frames]
    value = fact_wire(source)
    if rejected:
        value['frame_states'][3].update(visibility='absent', relations=[])
    content = provider.validate(value, actual_frames, selected, expected_version='3',
                                expected_coverage='v1', expected_grounding='v1')
    item = VisualProposal.objects.create(organization=team.org, session=selected, source_job_id=uuid.uuid4(),
                                         provider='local-test', model='no-model-call', content=content)
    return item, selected, actual_frames


@pytest.mark.django_db
def test_saved_grounded_proposal_read_is_immutable_and_apply_uses_exact_projection(team, draft):
    item, selected, actual_frames = saved_grounded_proposal(team, draft)
    original = deepcopy(item.content)
    working = selected.instruction.working_copy
    revision = working.revision
    old_steps = list(working.steps.values())
    once, twice = services.proposal_data(item), services.proposal_data(item)
    assert once == twice and once['can_apply'] is True
    assert {frame['id'] for frame in once['frames']} == {str(frame.id) for frame in actual_frames}
    item.refresh_from_db()
    working.refresh_from_db()
    assert item.content == original and item.applied_at is None
    assert working.revision == revision and list(working.steps.values()) == old_steps
    result = post(team.clients['author'], draft.url + f'/visual-proposals/{item.id}/apply',
                  {'confirm_replace': True}, etag=draft.working['etag'], key='apply-grounded')
    assert result.status_code == 200, result.json()
    fields = ('title', 'description', 'start_ms', 'end_ms', 'evidence_frame_ids')
    assert [{key: step[key] for key in fields} for step in result.json()['steps']] == [
        {key: step[key] for key in fields} for step in once['steps']]
    assert result.json()['steps'][0]['evidence_visual_session_id'] == str(selected.id)
    item.refresh_from_db()
    assert item.content == original and item.applied_at is not None
    assert not AIUsageReservation.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize('tamper', ['phase_text', 'missing_ledger', 'ledger_null', 'ledger_list'])
def test_saved_phase_tampering_and_missing_ledger_disable_read_apply(team, draft, tamper):
    from visual_drafts.teaching import describe_action
    item, selected, _ = saved_grounded_proposal(team, draft)
    if tamper == 'phase_text':
        # Keep ordinary step/observation identity valid so that the fact-ledger
        # check, rather than a mismatched cached description, catches this.
        observation = item.content['observations'][0]
        observation['before'] = 'Заготовка уже лежит на поверхности.'
        item.content['steps'][0]['description'] = describe_action(
            observation, language='ru', step_format=item.content['step_format'])
    elif tamper == 'missing_ledger':
        item.content.pop('frame_grounding')
    else:
        item.content['frame_grounding'] = None if tamper == 'ledger_null' else []
    # Original history is immutable. Store the malformed incoming content as a
    # separate proposal to exercise read/apply rejection without disabling guards.
    item = VisualProposal.objects.create(organization=team.org, session=selected, source_job_id=uuid.uuid4(),
                                         provider='local-test', model='no-model-call', content=item.content)
    original = deepcopy(item.content)
    working = selected.instruction.working_copy
    revision, old_steps = working.revision, list(working.steps.values())
    assert services.proposal_data(item)['can_apply'] is False
    assert services.proposal_data(item)['can_apply'] is False
    result = post(team.clients['author'], draft.url + f'/visual-proposals/{item.id}/apply',
                  {'confirm_replace': True}, etag=draft.working['etag'], key='reject-grounded-tamper')
    assert result.status_code == 422, result.json()
    item.refresh_from_db()
    working.refresh_from_db()
    assert item.content == original and item.applied_at is None
    assert working.revision == revision and list(working.steps.values()) == old_steps
    assert not AIUsageReservation.objects.exists()


@pytest.mark.django_db
def test_rejected_grounded_action_never_reappears_on_read_or_apply(team, draft):
    item, selected, actual_frames = saved_grounded_proposal(team, draft, rejected=True)
    original = deepcopy(item.content)
    assert original['steps'] == original['observations'] == []
    assert original['frame_grounding']['batches'][0]['rejected_observation_ids']
    working = selected.instruction.working_copy
    revision, old_steps = working.revision, list(working.steps.values())
    once, twice = services.proposal_data(item), services.proposal_data(item)
    assert once == twice and once['can_apply'] is False and once['steps'] == []
    assert {frame['id'] for frame in once['frames']} == {str(frame.id) for frame in actual_frames}
    result = post(team.clients['author'], draft.url + f'/visual-proposals/{item.id}/apply',
                  {'confirm_replace': True}, etag=draft.working['etag'], key='reject-grounded-empty')
    assert result.status_code == 422, result.json()
    assert result.json()['error']['code'] == 'NO_PROPOSED_STEPS'
    item.refresh_from_db()
    working.refresh_from_db()
    assert item.content == original and item.applied_at is None
    assert working.revision == revision and list(working.steps.values()) == old_steps
    assert not AIUsageReservation.objects.exists()


@pytest.mark.django_db
def test_evidence_ids_include_fact_frames_even_without_any_surviving_action(team, draft):
    item, _, actual_frames = saved_grounded_proposal(team, draft, rejected=True)
    expected = {str(frame.id) for frame in actual_frames}
    assert services.evidence_ids(item.content) == expected
    # Isolate the ledger from the other evidence carriers. The real stored
    # proposal remains untouched; each ledger carrier independently contributes.
    isolated = deepcopy(item.content)
    isolated['gaps'] = []
    isolated.pop('temporal_coverage')
    assert services.evidence_ids(isolated) == expected
    states_only = deepcopy(isolated)
    states_only['frame_grounding']['batches'][0]['frame_ids'] = []
    assert services.evidence_ids(states_only) == expected
    batch_only = deepcopy(isolated)
    batch_only['frame_grounding']['batches'][0]['states'] = []
    assert services.evidence_ids(batch_only) == expected
