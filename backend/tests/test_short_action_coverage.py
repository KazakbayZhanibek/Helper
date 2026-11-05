"""Temporal accounting and budget tests, not recognition-accuracy claims.

Model responses are local fixtures; no cloud transport is used in this file.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from ai_budget.models import AIUsageReservation, BudgetAccount
from ai_drafts import providers, codex_provider
from ai_drafts.providers import ProviderFailure
from ai_drafts.validation import InvalidModelOutput
from core.services import etag
from media_pipeline.exceptions import ProcessingFailure
from media_pipeline.models import ProcessingJob
from media_pipeline.worker import process_one
from visual_drafts import provider, services, short_actions, worker, review
from visual_drafts.models import VisualAnalysisReview, VisualProposal, VisualSession
from .conftest import post
from .test_action_protocol import action_value, images, instance
from .test_visual_drafts import add_frames
from .test_visual_full_video import full_video, fake_extraction, queue


def wire(source):
    value = action_value(source)
    value['schema_version'] = short_actions.WIRE_VERSION
    value['transitions'] = [
        {'pair_index': i, 'change': 'visible_action',
         'description': 'Рука перемещает удерживаемый предмет к следующему положению.',
         'observation_ids': ['o1']}
        for i in range(len(source) - 1)
    ]
    return value


def trusted_frames(source):
    return [SimpleNamespace(id=uuid.UUID(item['id']), sequence=item['sequence'],
                            timestamp_ms=item['timestamp_ms'],
                            coverage_start_ms=item['timestamp_ms'],
                            coverage_end_ms=item['timestamp_ms'] + 500)
            for item in source]


def session(mode='upload'):
    return SimpleNamespace(mode=mode, language='ru', asset=None,
                           window_start_ms=0, window_end_ms=12000)


def normalize(value, source, mode='upload'):
    return provider.validate(value, trusted_frames(source), session(mode),
                             expected_version='3', expected_coverage='v1')


def test_each_adjacent_transition_is_bound_to_trusted_times_and_instruction_links():
    source = images(4)
    value = wire(source)
    before = deepcopy(value)
    result = normalize(value, source)
    assert value == before
    assert result['schema_version'] == '3' and result['step_format'] == 'teaching_v1'
    assert len(result['steps']) == 1
    assert 'Как выполнить' in result['steps'][0]['description']
    audit = result['temporal_coverage']
    assert audit['version'] == 'v1' and audit['unresolved_pairs'] == 0
    assert len(audit['pairs']) == 3
    for index, row in enumerate(audit['pairs']):
        assert row['pair_index'] == index
        assert row['from_frame_id'] == source[index]['id']
        assert row['to_frame_id'] == source[index + 1]['id']
        assert row['gap_ms'] == 1000
        assert row['status'] == 'linked_action'
        assert row['observation_ids'] == result['steps'][0]['observation_ids']


@pytest.mark.parametrize('indices', [[0, 1], [0, 0, 2], [1, 0, 2], [0, 1, 2, 3]])
def test_missing_duplicate_reordered_or_extra_pair_rejects_coverage(indices):
    source = images(4)
    value = wire(source)
    value['transitions'] = [{**value['transitions'][0], 'pair_index': index} for index in indices]
    with pytest.raises(InvalidModelOutput, match='VISUAL_TRANSITION_COVERAGE_INVALID'):
        normalize(value, source)


def test_unknown_observation_link_is_rejected_even_when_frame_ids_are_valid():
    source = images(2)
    value = wire(source)
    value['transitions'][0]['observation_ids'] = ['o2']
    with pytest.raises(InvalidModelOutput, match='VISUAL_TRANSITION_LINK_INVALID'):
        normalize(value, source)


def test_foreign_frame_is_not_hidden_by_transition_coverage():
    source = images(2)
    value = wire(source)
    value['observations'][0]['action_frame_ids'][-1] = str(uuid.uuid4())
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_UNKNOWN'):
        normalize(value, source)


def test_cut_in_inventory_blocks_an_action_spanning_the_cut():
    source = images(4)
    value = wire(source)
    value['transitions'][1].update(change='cut', observation_ids=[])
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_CUT_INVALID'):
        normalize(value, source)


@pytest.mark.parametrize('change', ['visible_action', 'unexplained_change'])
def test_unlinked_transition_becomes_trusted_question_never_invented_instruction(change):
    source = images(2)
    value = wire(source)
    value['observations'] = []
    value['transitions'][0].update(change=change, observation_ids=[])
    result = normalize(value, source)
    assert result['steps'] == [] and result['observations'] == []
    assert result['temporal_coverage']['unresolved_pairs'] == 1
    gap = result['gaps'][0]
    assert gap['reason'] == 'unclear_action'
    assert gap['evidence_frame_ids'] == [item['id'] for item in source]
    assert (gap['start_ms'], gap['end_ms']) == (0, 1500)
    assert gap['description'].startswith('Уточните короткий переход по записи:')
    assert {'uncertain_action', 'insufficient_evidence'} <= {item['code'] for item in result['warnings']}


def test_state_link_is_not_promoted_to_an_action():
    source = images(2)
    value = wire(source)
    value['observations'][0].update(kind='state', movement=None, action_frame_ids=[],
                                    before='Предмет находится на поверхности.', before_frame_id=source[0]['id'])
    result = normalize(value, source)
    assert result['steps'] == []
    assert result['observations'][0]['kind'] == 'state'
    assert result['temporal_coverage']['pairs'][0]['status'] == 'unresolved'
    assert result['gaps'][0]['reason'] == 'unclear_action'


def test_phase_rejected_action_cannot_be_resurrected_by_inventory_link():
    source = images(2)
    value = wire(source)
    value['observations'][0]['action_frame_ids'] = [source[0]['id']]
    result = normalize(value, source)
    assert result['steps'] == [] and result['observations'] == []
    assert result['temporal_coverage']['pairs'][0]['observation_ids'] == []
    assert result['temporal_coverage']['unresolved_pairs'] == 1
    assert result['gaps']


def test_action_linked_to_the_wrong_pair_is_removed_without_repeating_its_command():
    source = images(4)
    value = wire(source)
    action = value['observations'][0]
    refs = [item['id'] for item in source[:2]]
    action.update(title='Поднимите инструмент', movement='Поднимите инструмент и отведите его влево.',
                  action_frame_ids=refs, evidence_frame_ids=refs,
                  before='Инструмент на поверхности.', after='Инструмент над поверхностью.',
                  before_frame_id=refs[0], after_frame_id=refs[-1])
    value['transitions'][0].update(change='unexplained_change', observation_ids=[])
    value['transitions'][1]['description'] = 'Поднимите инструмент и отведите его влево.'
    value['transitions'][2].update(change='no_clear_change', observation_ids=[])
    original = deepcopy(value)
    result = normalize(value, source)
    assert value == original
    assert result['steps'] == result['observations'] == []
    assert [row['status'] for row in result['temporal_coverage']['pairs']] == ['unresolved', 'unresolved', 'no_clear_change']
    assert result['temporal_coverage']['unresolved_pairs'] == 2
    assert all(row['observation_ids'] == [] for row in result['temporal_coverage']['pairs'])
    assert len(result['gaps']) == 2
    assert all(gap['description'] == short_actions.ACTION_LINK_REVIEW_REASON for gap in result['gaps'])
    rejected = result['gaps'][0]
    assert rejected['evidence_frame_ids'] == refs
    assert (rejected['start_ms'], rejected['end_ms']) == (0, 1500)
    assert {'uncertain_action', 'insufficient_evidence'} <= {item['code'] for item in result['warnings']}


@pytest.mark.parametrize('change', ['visible_action', 'unexplained_change', 'no_clear_change'])
def test_orphan_action_is_a_question_even_when_inventory_claims_no_clear_change(change):
    source = images(2)
    value = wire(source)
    value['transitions'][0].update(change=change, observation_ids=[])
    result = normalize(value, source)
    assert result['steps'] == result['observations'] == []
    assert result['temporal_coverage']['unresolved_pairs'] == 1
    assert result['temporal_coverage']['pairs'][0]['change'] == change
    assert result['temporal_coverage']['pairs'][0]['status'] == 'unresolved'
    assert len(result['gaps']) == 1
    assert result['gaps'][0]['description'] == short_actions.ACTION_LINK_REVIEW_REASON


def test_supported_cycle_survives_other_unexplained_and_unchanged_pairs():
    source = images(4)
    value = wire(source)
    value['transitions'][1].update(change='unexplained_change', observation_ids=[])
    value['transitions'][2].update(change='no_clear_change', observation_ids=[])
    result = normalize(value, source)
    assert len(result['steps']) == len(result['observations']) == 1
    assert result['steps'][0]['evidence_frame_ids'] == [item['id'] for item in source]
    assert result['observations'][0]['movement'] == value['observations'][0]['movement']
    assert [row['status'] for row in result['temporal_coverage']['pairs']] == ['linked_action', 'unresolved', 'no_clear_change']
    assert result['temporal_coverage']['unresolved_pairs'] == 1


def test_support_for_one_action_does_not_support_another_action_with_the_same_frames():
    source = images(2)
    value = wire(source)
    value['observations'].append({**deepcopy(value['observations'][0]), 'id': 'o2',
                                  'title': 'Унесите предмет', 'movement': 'Унесите предмет за край стола.'})
    result = normalize(value, source)
    assert len(result['steps']) == len(result['observations']) == 1
    assert result['observations'][0]['id'].endswith(':o1')
    assert result['temporal_coverage']['pairs'][0]['status'] == 'linked_action'
    assert result['temporal_coverage']['unresolved_pairs'] == 0
    assert len(result['gaps']) == 1
    assert result['gaps'][0]['description'] == short_actions.ACTION_LINK_REVIEW_REASON


def test_state_is_untouched_when_unlinked_action_is_removed():
    source = images(2)
    value = wire(source)
    state = {**deepcopy(value['observations'][0]), 'id': 'o2', 'kind': 'state',
             'movement': None, 'action_frame_ids': [],
             'before': 'Предмет лежит на поверхности.', 'before_frame_id': source[0]['id']}
    value['observations'].append(state)
    value['transitions'][0]['observation_ids'] = ['o2']
    result = normalize(value, source)
    assert result['steps'] == []
    assert len(result['observations']) == 1
    assert result['observations'][0]['kind'] == 'state'
    assert result['observations'][0]['before'] == state['before']
    assert result['observations'][0]['after'] == state['after']
    assert result['temporal_coverage']['pairs'][0]['observation_ids'] == [result['observations'][0]['id']]
    assert result['temporal_coverage']['unresolved_pairs'] == 1


def test_unlinked_action_cannot_hide_a_cut_violation():
    source = images(2)
    value = wire(source)
    value['transitions'][0].update(change='cut', observation_ids=[])
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_CUT_INVALID'):
        normalize(value, source)


@pytest.mark.parametrize('corruption', ['foreign_frame', 'reordered_frames'])
def test_unlinked_action_still_rejects_invalid_frame_references_before_filtering(corruption):
    source = images(2)
    value = wire(source)
    value['transitions'][0]['observation_ids'] = []
    if corruption == 'foreign_frame':
        value['observations'][0]['action_frame_ids'][-1] = str(uuid.uuid4())
    else:
        value['observations'][0]['action_frame_ids'].reverse()
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_(UNKNOWN|ORDER_INVALID)'):
        normalize(value, source)


def test_no_clear_change_is_retained_as_model_inventory_not_instruction_or_verified_badge():
    source = images(2)
    value = wire(source)
    value['observations'] = []
    value['transitions'][0].update(change='no_clear_change', observation_ids=[])
    result = normalize(value, source)
    assert result['steps'] == result['observations'] == result['gaps'] == []
    assert result['temporal_coverage']['pairs'][0]['status'] == 'no_clear_change'
    assert 'insufficient_evidence' in {item['code'] for item in result['warnings']}
    assert 'verified' not in result and 'can_apply' not in result


def test_unexplained_transition_does_not_duplicate_existing_gap():
    source = images(2)
    value = wire(source)
    value['observations'] = []
    value['transitions'][0].update(change='unexplained_change', observation_ids=[])
    value['gaps'] = [{'reason': 'occluded', 'description': 'Рука закрывает способ перехода.',
                     'evidence_frame_ids': [item['id'] for item in source]}]
    result = normalize(value, source)
    assert len(result['gaps']) == 1 and result['gaps'][0]['reason'] == 'occluded'
    assert result['temporal_coverage']['unresolved_pairs'] == 1


@pytest.mark.parametrize('mutation', ['single', 'thirteen', 'duplicate_id', 'reversed_time', 'duplicate_sequence', 'boolean_time'])
def test_temporal_input_rejects_unbounded_or_ambiguous_frame_order(mutation):
    source = images(3)
    if mutation == 'single': source = source[:1]
    elif mutation == 'thirteen': source = images(13)
    elif mutation == 'duplicate_id': source[1]['id'] = source[0]['id']
    elif mutation == 'reversed_time': source[1]['timestamp_ms'] = -1
    elif mutation == 'duplicate_sequence': source[1]['sequence'] = source[0]['sequence']
    elif mutation == 'boolean_time': source[0]['timestamp_ms'] = False
    with pytest.raises(ProviderFailure, match='VISUAL_EVIDENCE_INVALID'):
        short_actions.temporal_pairs(source)


@pytest.mark.parametrize('mode,protocol,pin', [('live', '3', 'v1'), ('upload', '2', 'v1'), ('upload', '3', 'v2')])
def test_coverage_contract_is_upload_v3_and_known_version_only(mode, protocol, pin):
    with pytest.raises(ProviderFailure, match='VISUAL_PROTOCOL_INVALID'):
        provider.request_contract(protocol, 'ru', mode, images(2), short_action_coverage=pin)


def test_output_requires_matching_pin_and_live_cannot_normalize_teaching_inventory():
    source = images(2)
    with pytest.raises(InvalidModelOutput, match='VISUAL_OUTPUT_INVALID'):
        provider.validate(wire(source), trusted_frames(source), session(), expected_version='3')
    with pytest.raises(InvalidModelOutput, match='VISUAL_OUTPUT_INVALID'):
        normalize(action_value(source), source)
    with pytest.raises(InvalidModelOutput, match='VISUAL_PROTOCOL_INVALID'):
        normalize(wire(source), source, mode='live')


def test_completed_checkpoint_cannot_drop_the_temporal_contract():
    selected = SimpleNamespace(mode='upload', analysis_budget={'visual_protocol': '3', 'short_action_coverage': 'v1'})
    job = SimpleNamespace(payload={'visual_protocol': '3', 'short_action_coverage': 'v1'})
    checkpoint = SimpleNamespace(state='completed', content={'schema_version': '3'})
    with pytest.raises(ProcessingFailure) as failure:
        worker.run_window(job, selected, checkpoint, None, None)
    assert failure.value.code == 'VISUAL_PROTOCOL_INVALID'


def test_combined_inventory_and_observations_keep_the_raw_output_ceiling():
    source = images(12)
    value = wire(source)
    observation = value['observations'][0]
    value['observations'] = [{**observation, 'id': f'o{i}', 'movement': 'я' * 800} for i in range(1, 13)]
    for item in value['observations']:
        item.update(subject='я' * 120, title='я' * 120, before='я' * 240,
                    before_frame_id=source[0]['id'], after='я' * 240, after_frame_id=source[-1]['id'])
    for row in value['transitions']:
        row['description'] = 'я' * 240
    assert len(json.dumps(value, ensure_ascii=False).encode()) > 32768
    with pytest.raises(InvalidModelOutput, match='VISUAL_OUTPUT_INVALID'):
        normalize(value, source)


@pytest.mark.parametrize('backend', ['codex', 'openai'])
def test_real_twelve_jpeg_preflight_fits_budget_and_binds_actual_pair_count(backend):
    root = Path(__file__).resolve().parents[2]
    files = sorted((root / 'artifacts/test-media/real-benchmark/food/kneading-review-frames').glob('*.jpg'))[:12]
    if len(files) != 12:
        pytest.skip('Licensed local benchmark JPEGs are not installed in this checkout')
    source = []
    for index, path in enumerate(files):
        raw = path.read_bytes()
        source.append({'id': str(uuid.uuid4()), 'sequence': index + 1, 'timestamp_ms': index * 500,
                       'bytes': raw, 'sha256': hashlib.sha256(raw).hexdigest()})
    item = instance(backend)
    item.short_action_coverage = 'v1'
    system, schema, request, body = item.prepare_request(language='ru', mode='upload', frames=source)
    assert len(json.dumps({'system': system, 'schema': schema, 'request': request}, ensure_ascii=False).encode()) + 1024 <= 32768
    assert len(request['frames']) == 12 and len(request['temporal_pairs']) == 11
    assert schema['properties']['transitions']['minItems'] == schema['properties']['transitions']['maxItems'] == 11
    assert Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(wire(source))
    if body:
        assert len([part for part in body['messages'][1]['content'] if part['type'] == 'image_url']) == 12
        assert body['max_completion_tokens'] == 8192


def test_new_request_preserves_shared_legacy_schemas_and_rebinds_every_frame_id():
    old_schema = deepcopy(provider.SCHEMA)
    original = deepcopy(short_actions.SCHEMA)
    first, second = images(3), images(2)
    _, first_schema = provider.request_contract('3', 'ru', 'upload', first, short_action_coverage='v1')
    _, second_schema = provider.request_contract('3', 'kk', 'upload', second, short_action_coverage='v1')
    assert first_schema['properties']['transitions']['minItems'] == 2
    assert second_schema['properties']['transitions']['minItems'] == 1
    assert first[0]['id'] not in json.dumps(second_schema)
    assert short_actions.SCHEMA == original and provider.SCHEMA == old_schema
    for protocol in ('2', '3'):
        _, schema = provider.request_contract(protocol, 'ru', 'upload', second)
        assert schema['properties']['schema_version']['enum'] == [protocol]
        assert 'transitions' not in schema['properties']


@pytest.fixture
def coverage_video(full_video, settings, monkeypatch):
    settings.VISUAL_ACTION_EVIDENCE = True
    settings.VISUAL_SHORT_ACTION_COVERAGE = True
    settings.VISUAL_SEMANTIC_REVIEW = True  # New single-call mode takes precedence.
    configuration = lambda org: {'available': True, 'provider': 'codex', 'model': 'gpt-6-luna'}
    monkeypatch.setattr(services, 'capabilities', configuration)
    monkeypatch.setattr(worker, 'capabilities', configuration)
    monkeypatch.setattr(providers, 'endpoint_configuration', configuration)
    return full_video


def install_model(monkeypatch, *, reject=False, wrong_wire=False):
    calls = []

    class LocalModel(provider.VisualProvider):
        def __init__(self, organization):
            self.provider, self.model, self.endpoint = 'codex', 'gpt-6-luna', ''

        def prepare_request(self, **kwargs):
            if reject:
                raise ProviderFailure('AI_BUDGET_INPUT_UNBOUNDED', 'fixture input rejected')
            return super().prepare_request(**kwargs)

        def generate(self, *, language, mode, frames, context=None):
            calls.append((mode, self.protocol, self.short_action_coverage))
            if self.protocol == '2':
                return {'schema_version': '2', 'language': language, 'steps': [
                    {'title': 'Переместите предмет', 'description': 'Сдвиньте предмет рукой вправо.',
                     'evidence_frame_ids': [frame['id'] for frame in frames]}], 'warnings': []}
            return wire(frames) if self.short_action_coverage and not wrong_wire else action_value(frames)

    monkeypatch.setattr(worker, 'VisualProvider', LocalModel)
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
    monkeypatch.setattr(review, 'VisualReviewProvider', lambda *a, **k: pytest.fail('Second model call was attempted'))
    monkeypatch.setattr(codex_provider, 'generate', lambda **k: pytest.fail('External CLI was attempted'))
    monkeypatch.setattr(provider, 'json_request', lambda *a, **k: pytest.fail('External HTTP was attempted'))
    return calls


@pytest.mark.django_db
def test_worker_uses_one_real_ledger_unit_and_keeps_temporal_audit_in_result(team, coverage_video, monkeypatch):
    calls = install_model(monkeypatch)
    job, selected = queue(team, coverage_video, scope='window', start_ms=0, end_ms=20000, max_ai_calls=1)
    assert selected.analysis_budget['short_action_coverage'] == job.payload['short_action_coverage'] == 'v1'
    assert 'semantic_review' not in selected.analysis_budget and 'semantic_review' not in job.payload
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done', (job.error_code, job.error_summary)
    assert calls == [('upload', '3', 'v1')]
    receipt = AIUsageReservation.objects.get(session_id=selected.id)
    assert receipt.state == 'settled' and receipt.unit == 'calls' and receipt.max_units == 1
    assert BudgetAccount.objects.get(key=f'calls:session:{selected.id}').spent == 1
    assert not VisualAnalysisReview.objects.exists()
    result = VisualProposal.objects.get(source_job_id=job.id).content
    assert result['schema_version'] == '3' and result['step_format'] == 'teaching_v1'
    assert len(result['temporal_coverage']['pairs']) == 1
    assert result['temporal_coverage']['pairs'][0]['status'] == 'linked_action'
    assert not coverage_video.working.steps.exists()


@pytest.mark.django_db
@pytest.mark.parametrize('enabled', [True, False])
def test_session_pin_survives_runtime_flag_changes(team, coverage_video, settings, monkeypatch, enabled):
    settings.VISUAL_SEMANTIC_REVIEW = False
    settings.VISUAL_SHORT_ACTION_COVERAGE = enabled
    calls = install_model(monkeypatch)
    job, selected = queue(team, coverage_video, scope='window', start_ms=0, end_ms=20000, max_ai_calls=1)
    assert selected.analysis_budget.get('short_action_coverage') == ('v1' if enabled else None)
    settings.VISUAL_SHORT_ACTION_COVERAGE = not enabled
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done'
    assert calls == [('upload', '3', 'v1' if enabled else None)]
    content = VisualProposal.objects.get(source_job_id=job.id).content
    assert ('temporal_coverage' in content) is enabled
    assert AIUsageReservation.objects.count() == 1


@pytest.mark.django_db
def test_pin_mismatch_fails_before_model_or_budget_reservation(team, coverage_video, monkeypatch):
    calls = install_model(monkeypatch)
    job, selected = queue(team, coverage_video, scope='window', start_ms=0, end_ms=20000, max_ai_calls=1)
    mismatched = deepcopy(job.payload)
    mismatched.pop('short_action_coverage')
    with pytest.raises(ProcessingFailure) as error:
        worker.protocol_for_job(SimpleNamespace(payload=mismatched), selected)
    assert error.value.code == 'VISUAL_PROTOCOL_INVALID'
    assert calls == [] and not AIUsageReservation.objects.exists()


@pytest.mark.django_db
def test_deterministic_input_rejection_is_free_and_does_not_dispatch(team, coverage_video, monkeypatch):
    calls = install_model(monkeypatch, reject=True)
    job, selected = queue(team, coverage_video, scope='window', start_ms=0, end_ms=20000, max_ai_calls=1)
    assert process_one()
    assert calls == [] and not AIUsageReservation.objects.exists()
    assert VisualProposal.objects.get(source_job_id=job.id).content['steps'] == []


@pytest.mark.django_db
def test_wrong_model_contract_spends_once_without_fallback_or_hidden_retry(team, coverage_video, monkeypatch):
    calls = install_model(monkeypatch, wrong_wire=True)
    job, selected = queue(team, coverage_video, scope='window', start_ms=0, end_ms=20000, max_ai_calls=1)
    assert process_one()
    assert calls == [('upload', '3', 'v1')]
    checkpoint = selected.analysis_windows.get()
    assert checkpoint.state == 'failed' and checkpoint.error_code == 'VISUAL_OUTPUT_INVALID'
    assert VisualProposal.objects.get(source_job_id=job.id).content['steps'] == []
    assert AIUsageReservation.objects.filter(state='settled').count() == 1
    assert not VisualAnalysisReview.objects.exists()


@pytest.mark.django_db
def test_live_keeps_existing_factual_contract_even_with_coverage_flag(team, coverage_video, monkeypatch):
    calls = install_model(monkeypatch)
    response = post(team.clients['author'], coverage_video.url + '/visual-sessions', {'consent_images': True},
                    etag=etag(coverage_video.working), key='short-live')
    assert response.status_code == 201, response.json()
    selected = VisualSession.objects.get(pk=response.json()['id'])
    add_frames(selected)
    response = post(team.clients['author'], f'/api/v1/orgs/{team.org.id}/visual-sessions/{selected.id}/analyze', {}, key='short-live-analysis')
    assert response.status_code == 202, response.json()
    job = ProcessingJob.objects.get(pk=response.json()['id'])
    assert process_one()
    assert calls == [('live', '3', None)]
    assert 'short_action_coverage' not in selected.analysis_budget and 'short_action_coverage' not in job.payload
    content = VisualProposal.objects.get(source_job_id=job.id).content
    assert 'temporal_coverage' not in content and 'step_format' not in content


@pytest.mark.django_db
def test_legacy_upload_does_not_opt_into_new_contract(team, coverage_video, settings, monkeypatch):
    settings.VISUAL_ACTION_EVIDENCE = False
    calls = install_model(monkeypatch)
    job, selected = queue(team, coverage_video, scope='window', start_ms=0, end_ms=20000, max_ai_calls=1)
    assert process_one()
    assert calls == [('upload', '2', None)]
    assert 'short_action_coverage' not in selected.analysis_budget
    assert VisualProposal.objects.get(source_job_id=job.id).content['schema_version'] == '2'


def test_aggregation_retains_pair_evidence_even_when_no_action_was_named(monkeypatch):
    from visual_drafts import planning
    source = images(4)
    value = wire(source)
    value['observations'] = []
    for row in value['transitions']:
        row.update(change='no_clear_change', observation_ids=[])
    content = normalize(value, source)
    snapshot = deepcopy(content)
    window_id = uuid.uuid4()
    window = SimpleNamespace(id=window_id, parent_id=None, state='completed', content=content)
    selected = session()
    selected.analysis_budget = {'visual_protocol': '3', 'short_action_coverage': 'v1'}
    monkeypatch.setattr(planning, 'analysis_data', lambda value: {})
    result = planning.aggregate_content(selected, [window])
    assert result['steps'] == []
    assert {row['window_id'] for row in result['temporal_coverage']['pairs']} == {str(window_id)}
    assert services.evidence_ids(result) == {item['id'] for item in source}
    result['temporal_coverage']['pairs'][0]['observation_ids'].append('mutated')
    assert content == snapshot
