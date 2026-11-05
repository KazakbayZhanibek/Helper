"""Versioned indexed grounding integration; all inference stays local."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch
import uuid

import pytest
from django.db import IntegrityError, transaction
from jsonschema import Draft202012Validator

from ai_budget.models import AIUsageReservation
from ai_drafts import codex_provider
from ai_drafts.providers import ProviderFailure
from ai_drafts.validation import InvalidModelOutput
from media_pipeline.exceptions import ProcessingFailure
from media_pipeline.models import ProcessingJob
from media_pipeline.worker import JobContext, process_one
from visual_drafts import planning, provider, services, worker
from visual_drafts.models import VisualProposal, VisualSession
from .conftest import post
from .test_action_protocol import images, instance
from .test_grounding_pipeline import saved_grounded_proposal
from .test_instruction_photos import evidence as saved_evidence
from .test_short_action_coverage import trusted_frames, session
from .test_visual_full_video import full_video, queue, fake_extraction


def pinned_session(pin='v2'):
    selected = session()
    selected.analysis_budget = {'visual_protocol': '3', 'short_action_coverage': 'v1', 'frame_grounding': pin}
    return selected


def indexed_provider(backend='codex'):
    selected = instance(backend)
    selected.short_action_coverage, selected.frame_grounding = 'v1', 'v2'
    return selected


def enable(settings):
    settings.VISUAL_ACTION_EVIDENCE = settings.VISUAL_SHORT_ACTION_COVERAGE = settings.VISUAL_FRAME_GROUNDING = True
    settings.VISUAL_FRAME_GROUNDING_VERSION = 'v2'


def wire(source, *, motion='observed'):
    return {'schema_version': '3-grounded-actions-v2', 'language': 'ru', 'cut_after_image_indices': [],
            'actions': [{'subject': 'Заготовка', 'title': 'Опустите заготовку',
                         'movement': 'Удерживайте заготовку рукой и опустите на поверхность.',
                         'motion': motion, 'evidence': [
                {'image_index': index, 'subject_visibility': 'visible', 'relations': [
                    {'kind': 'held_by' if index == 1 else 'supported_by',
                     'target': 'Рука' if index == 1 else 'Поверхность',
                     'target_visibility': 'visible', 'status': 'observed'}]}
                for index in range(1, len(source) + 1)]}]}


def normalized(source, *, motion='observed'):
    return provider.validate(wire(source, motion=motion), trusted_frames(source), pinned_session(),
                             expected_version='3', expected_coverage='v1', expected_grounding='v2')


@pytest.mark.parametrize('job_pin,session_pin', [('v2', 'v1'), ('v1', 'v2'), (None, 'v2'), ('v2', None), ('v3', 'v3')])
def test_contract_versions_cannot_be_upgraded_or_downgraded(job_pin, session_pin):
    selected = pinned_session(session_pin)
    with pytest.raises(ProcessingFailure):
        worker.protocol_for_job(SimpleNamespace(payload={**selected.analysis_budget, 'frame_grounding': job_pin}), selected)


@pytest.mark.parametrize('pin', ['v1', 'v2'])
def test_both_grounded_versions_keep_their_pinned_mode_and_disable_refinements(pin):
    selected = pinned_session(pin)
    assert worker.protocol_for_job(SimpleNamespace(payload=deepcopy(selected.analysis_budget)), selected) == '3'
    assert not planning.needs_refinement({'schema_version': '3', 'steps': [],
        'gaps': [{'reason': 'unclear_action'}], 'frame_grounding': {'version': pin, 'batches': []}})


@pytest.mark.parametrize('grounding', [None, [], {'version': 'v1', 'batches': [{}]},
                                      {'version': 'v2'}, {'version': 'v2', 'batches': []}])
def test_completed_checkpoint_rejects_missing_or_different_grounding(grounding):
    selected = pinned_session()
    content = {'schema_version': '3', 'temporal_coverage': {'version': 'v1'}}
    if grounding is not None:
        content['frame_grounding'] = grounding
    with pytest.raises(ProcessingFailure):
        worker.run_window(SimpleNamespace(payload=deepcopy(selected.analysis_budget)), selected,
                          SimpleNamespace(state='completed', content=content), None, None)


@pytest.mark.parametrize('backend', ['codex', 'openai'])
@pytest.mark.parametrize('count', [2, 12])
def test_indexed_request_binds_actual_images_without_uuid_or_transition_output(backend, count):
    source = images(count)
    system, schema, request, body = indexed_provider(backend).prepare_request(language='ru', mode='upload', frames=source)
    assert schema['properties']['schema_version']['enum'] == ['3-grounded-actions-v2']
    assert request['frames'] == [{'image_index': index, 'timestamp_ms': frame['timestamp_ms']}
                                 for index, frame in enumerate(source, 1)]
    assert 'temporal_pairs' not in request and 'transitions' not in schema['properties']
    index_schema = schema['properties']['actions']['items']['properties']['evidence']['items']['properties']['image_index']
    assert index_schema['enum'] == list(range(1, count + 1))
    assert schema['properties']['cut_after_image_indices']['items']['enum'] == list(range(1, count))
    rendered = json.dumps({'system': system, 'schema': schema, 'request': request}, ensure_ascii=False)
    assert all(frame['id'] not in rendered for frame in source)
    assert len(rendered.encode()) + 1024 <= 32768
    if body:
        assert body['max_completion_tokens'] == 8192
        items = body['messages'][1]['content']
        assert len([item for item in items if item['type'] == 'image_url']) == count
        labels = [item['text'] for item in items[1:] if item['type'] == 'text']
        assert labels == [f"image_index={index}; timestamp_ms={frame['timestamp_ms']}" for index, frame in enumerate(source, 1)]


@pytest.mark.django_db
def test_new_sessions_pin_v2_at_creation_and_feature_changes_leave_the_contract_intact(team, full_video, settings):
    enable(settings)
    job, selected = queue(team, full_video, max_ai_calls=3)
    assert selected.analysis_budget['frame_grounding'] == job.payload['frame_grounding'] == 'v2'
    assert provider.capabilities(team.org)['automatic_refinements'] is False
    settings.VISUAL_FRAME_GROUNDING_VERSION = 'v1'
    settings.VISUAL_FRAME_GROUNDING = False
    assert worker.protocol_for_job(job, selected) == '3'
    assert worker.provider_for_job(job, selected).frame_grounding == 'v2'
    selected.analysis_budget['frame_grounding'] = 'v1'
    with pytest.raises(IntegrityError), transaction.atomic():
        selected.save(update_fields=['analysis_budget'])
    selected.refresh_from_db()
    assert selected.analysis_budget['frame_grounding'] == 'v2'


@pytest.mark.django_db
def test_unknown_new_session_version_fails_before_creating_job_or_receipt(team, full_video, settings):
    from core.services import etag
    enable(settings)
    settings.VISUAL_FRAME_GROUNDING_VERSION = 'future'
    result = post(team.clients['author'], full_video.url + '/visual-proposals',
                  {'consent_images': True, 'scope': 'full'}, etag=etag(full_video.working), key='unknown-version')
    assert result.status_code == 422 and result.json()['error']['code'] == 'VISUAL_PROTOCOL_INVALID'
    assert not VisualSession.objects.exists() and not ProcessingJob.objects.exists()
    assert not AIUsageReservation.objects.exists()


@pytest.mark.django_db
def test_enabling_v2_does_not_rewrite_or_reinterpret_v1_history(team, draft, settings):
    item, selected, _ = saved_grounded_proposal(team, draft)
    before, original = services.proposal_data(item), deepcopy(item.content)
    enable(settings)
    assert services.proposal_data(item) == before
    assert selected.analysis_budget['frame_grounding'] == 'v1'
    item.refresh_from_db()
    assert item.content == original and item.applied_at is None
    assert not AIUsageReservation.objects.exists()


def test_same_indexed_contract_and_image_bytes_use_one_request_per_transport(monkeypatch):
    source, captured = images(12), {}
    answer = wire(source)

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
    for backend in ('codex', 'openai'):
        assert indexed_provider(backend).generate(language='ru', mode='upload', frames=source) == answer
    body, request = captured['http'], captured['cli']
    assert body['messages'][0]['content'] == request['system']
    assert body['response_format']['json_schema']['schema'] == request['schema']
    assert request['frames'] is source and request['reasoning_effort'] == 'medium'
    assert request['max_output_bytes'] == 32768 and body['max_completion_tokens'] == 8192
    assert Draft202012Validator(request['schema']).is_valid(answer)
    assert len(normalized(source)['steps']) == 1


@pytest.mark.parametrize('corruption', ['missing_version', 'v1_wire', 'unknown_index'])
def test_new_validator_cannot_accept_wrong_response_version_or_unknown_image(corruption):
    source = images(2)
    value = wire(source)
    if corruption == 'missing_version':
        value.pop('schema_version')
    elif corruption == 'v1_wire':
        from .test_frame_grounding import wire as v1_wire
        value = v1_wire(source)
    else:
        value['actions'][0]['evidence'][1]['image_index'] = 3
    with pytest.raises(InvalidModelOutput):
        provider.validate(value, trusted_frames(source), pinned_session(),
                          expected_version='3', expected_coverage='v1', expected_grounding='v2')


def test_aggregation_keeps_all_indexed_batches_including_rejected_actions(monkeypatch):
    monkeypatch.setattr(planning, 'analysis_data', lambda _: {})
    first, second = normalized(images(3)), normalized(images(3), motion='unclear')
    original = deepcopy([first, second])
    windows = [SimpleNamespace(id=uuid.uuid4(), parent_id=None, state='completed', content=value)
               for value in (first, second)]
    result = planning.aggregate_content(pinned_session(), windows)
    assert len(result['steps']) == 1 and result['gaps']
    assert result['frame_grounding']['version'] == 'v2'
    assert result['frame_grounding']['batches'] == [value['frame_grounding']['batches'][0] for value in (first, second)]
    assert result['frame_grounding']['batches'][1]['rejected_observation_ids']
    result['frame_grounding']['batches'][1]['actions'][0]['movement'] = 'mutated'
    assert [first, second] == original
    assert 'refinement_incomplete' not in {warning['code'] for warning in result['warnings']}


@pytest.mark.parametrize('corruption', ['v1', 'missing_ledger', 'wrong_batch_fields', 'conflicting_batch'])
def test_aggregate_fails_closed_on_mixed_versions_or_damaged_indexed_batches(monkeypatch, corruption):
    monkeypatch.setattr(planning, 'analysis_data', lambda _: {})
    first, second = normalized(images(2)), normalized(images(2))
    if corruption == 'v1':
        second['frame_grounding']['version'] = 'v1'
    elif corruption == 'missing_ledger':
        second.pop('frame_grounding')
    elif corruption == 'wrong_batch_fields':
        second['frame_grounding']['batches'][0]['actions'] = None
    else:
        second['frame_grounding']['batches'][0]['id'] = first['frame_grounding']['batches'][0]['id']
    windows = [SimpleNamespace(id=uuid.uuid4(), parent_id=None, state='completed', content=value)
               for value in (first, second)]
    with pytest.raises(ProcessingFailure):
        planning.aggregate_content(pinned_session(), windows)


@pytest.mark.django_db
@pytest.mark.parametrize('result_kind', ['actions', 'unclear', 'invalid'])
def test_worker_never_spends_more_than_the_three_overviews_for_any_model_outcome(team, full_video, settings, monkeypatch, result_kind):
    enable(settings)
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
    monkeypatch.setattr(JobContext, 'call', lambda self, function, timeout: function())
    calls = []

    class LocalModel(provider.VisualProvider):
        def __init__(self, organization):
            self.provider, self.model, self.endpoint = 'openai', 'gpt-5.4-mini', 'https://api.openai.com/v1'

        def generate(self, *, language, mode, frames, context=None):
            assert self.protocol == '3' and self.short_action_coverage == 'v1' and self.frame_grounding == 'v2'
            calls.append(frames[0]['timestamp_ms'])
            self._usage_recorder({'prompt_tokens': 1000, 'completion_tokens': 100}, f'indexed-{len(calls)}')
            value = wire(frames, motion='unclear' if result_kind == 'unclear' else 'observed')
            if result_kind == 'invalid':
                value['actions'][0]['evidence'][0]['image_index'] = 99
            return value

    monkeypatch.setattr(worker, 'VisualProvider', LocalModel)
    job, selected = queue(team, full_video, max_ai_calls=6)
    settings.VISUAL_FRAME_GROUNDING_VERSION = 'v1'
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done', (job.error_code, job.error_summary)
    assert calls == [0, 30000, 60000]
    assert AIUsageReservation.objects.count() == AIUsageReservation.objects.filter(state='settled').count() == 3
    assert not selected.analysis_windows.filter(parent__isnull=False).exists()
    content = VisualProposal.objects.get(source_job_id=job.id).content
    assert content['frame_grounding']['version'] == 'v2'
    assert not full_video.working.steps.exists()
    if result_kind == 'actions':
        assert len(content['steps']) == 3
    elif result_kind == 'unclear':
        assert not content['steps'] and content['gaps']
        assert all(batch['rejected_observation_ids'] for batch in content['frame_grounding']['batches'])
    else:
        assert not content['steps'] and not content['frame_grounding']['batches']
        assert all(window['error_code'] == 'VISUAL_OUTPUT_INVALID' for window in content['analysis']['windows'])


@pytest.mark.django_db
def test_oversized_indexed_prompt_stops_before_reserving_a_model_call(team, full_video, settings, monkeypatch):
    from visual_drafts import grounding_v2
    enable(settings)
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
    monkeypatch.setattr(grounding_v2, 'SYSTEM', 'я' * 32768)

    class NoTransport(provider.VisualProvider):
        def __init__(self, organization):
            self.provider, self.model, self.endpoint = 'openai', 'gpt-5.4-mini', 'https://api.openai.com/v1'

        def generate(self, **kwargs):
            pytest.fail('Input rejected locally must not reach a model')

    monkeypatch.setattr(worker, 'VisualProvider', NoTransport)
    job, selected = queue(team, full_video, max_ai_calls=6)
    assert process_one()
    assert not AIUsageReservation.objects.exists()
    assert not selected.analysis_windows.filter(parent__isnull=False).exists()
    assert VisualProposal.objects.get(source_job_id=job.id).content['frame_grounding'] == {'version': 'v2', 'batches': []}


def saved_indexed_proposal(team, draft, *, rejected=False):
    create = VisualSession.objects.create
    with patch.object(VisualSession.objects, 'create', side_effect=lambda **kwargs: create(
            **kwargs, analysis_budget=deepcopy(pinned_session().analysis_budget))):
        selected, frames = saved_evidence(team, draft, count=3)
    value = wire(frames, motion='unclear' if rejected else 'observed')
    content = provider.validate(value, frames, selected, expected_version='3', expected_coverage='v1', expected_grounding='v2')
    item = VisualProposal.objects.create(organization=team.org, session=selected, source_job_id=uuid.uuid4(),
                                         provider='local-test', model='no-model-call', content=content)
    return item, selected, frames


@pytest.mark.django_db
def test_saved_indexed_projection_is_repeatable_and_applied_exactly(team, draft):
    item, selected, frames = saved_indexed_proposal(team, draft)
    original = deepcopy(item.content)
    once, twice = services.proposal_data(item), services.proposal_data(item)
    assert once == twice and once['can_apply'] is True
    assert {frame['id'] for frame in once['frames']} == {str(frame.id) for frame in frames}
    result = post(team.clients['author'], draft.url + f'/visual-proposals/{item.id}/apply',
                  {'confirm_replace': True}, etag=draft.working['etag'], key='apply-indexed')
    assert result.status_code == 200, result.json()
    fields = ('title', 'description', 'start_ms', 'end_ms', 'evidence_frame_ids')
    assert [{key: step[key] for key in fields} for step in result.json()['steps']] == [
        {key: step[key] for key in fields} for step in once['steps']]
    item.refresh_from_db()
    assert item.content == original and item.applied_at is not None
    assert not AIUsageReservation.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize('tamper', ['v1_ledger', 'missing_ledger', 'phase_text', 'ledger_action', 'null_ledger', 'list_ledger'])
def test_tampered_indexed_result_cannot_be_applied_or_rewritten(team, draft, tamper):
    from visual_drafts.teaching import describe_action
    item, selected, _ = saved_indexed_proposal(team, draft)
    if tamper == 'v1_ledger':
        item.content['frame_grounding']['version'] = 'v1'
    elif tamper == 'missing_ledger':
        item.content.pop('frame_grounding')
    elif tamper == 'phase_text':
        item.content['observations'][0]['before'] = 'Готовая заготовка лежит на поверхности.'
        item.content['steps'][0]['description'] = describe_action(item.content['observations'][0], language='ru', step_format='teaching_v1')
    elif tamper == 'ledger_action':
        item.content['frame_grounding']['batches'][0]['actions'][0]['movement'] = 'Бросьте заготовку.'
    else:
        item.content['frame_grounding'] = None if tamper == 'null_ledger' else []
    item = VisualProposal.objects.create(organization=team.org, session=selected, source_job_id=uuid.uuid4(),
                                         provider='local-test', model='no-model-call', content=item.content)
    original = deepcopy(item.content)
    assert services.proposal_data(item)['can_apply'] is False
    result = post(team.clients['author'], draft.url + f'/visual-proposals/{item.id}/apply',
                  {'confirm_replace': True}, etag=draft.working['etag'], key='reject-indexed')
    assert result.status_code == 422, result.json()
    item.refresh_from_db()
    assert item.content == original and item.applied_at is None
    assert not AIUsageReservation.objects.exists()


@pytest.mark.django_db
def test_rejected_indexed_action_stays_rejected_but_all_photos_remain_available(team, draft):
    item, selected, frames = saved_indexed_proposal(team, draft, rejected=True)
    original = deepcopy(item.content)
    once, twice = services.proposal_data(item), services.proposal_data(item)
    assert once == twice and once['steps'] == [] and once['can_apply'] is False
    assert once['gaps'] and item.content['frame_grounding']['batches'][0]['rejected_observation_ids']
    assert {frame['id'] for frame in once['frames']} == {str(frame.id) for frame in frames}
    isolated = deepcopy(item.content)
    isolated['gaps'] = []
    isolated.pop('temporal_coverage')
    assert services.evidence_ids(isolated) == {str(frame.id) for frame in frames}
    result = post(team.clients['author'], draft.url + f'/visual-proposals/{item.id}/apply',
                  {'confirm_replace': True}, etag=draft.working['etag'], key='reject-empty-indexed')
    assert result.status_code == 422 and result.json()['error']['code'] == 'NO_PROPOSED_STEPS'
    item.refresh_from_db()
    assert item.content == original and item.applied_at is None
    assert not AIUsageReservation.objects.exists()


@pytest.mark.django_db
def test_saved_v1_session_cannot_acquire_an_indexed_ledger(team, draft):
    item, selected, frames = saved_grounded_proposal(team, draft)
    item.content['frame_grounding'] = {'version': 'v2', 'batches': [{
        'id': 'b-test', 'frame_ids': [str(frame.id) for frame in frames],
        'actions': wire(frames)['actions'], 'cut_after_image_indices': [], 'rejected_observation_ids': [],
    }]}
    item = VisualProposal.objects.create(organization=team.org, session=selected, source_job_id=uuid.uuid4(),
                                         provider='local-test', model='no-model-call', content=item.content)
    assert services.proposal_data(item)['can_apply'] is False
    result = post(team.clients['author'], draft.url + f'/visual-proposals/{item.id}/apply',
                  {'confirm_replace': True}, etag=draft.working['etag'], key='reject-upgraded-ledger')
    assert result.status_code == 422 and selected.analysis_budget['frame_grounding'] == 'v1'
    assert not AIUsageReservation.objects.exists()
