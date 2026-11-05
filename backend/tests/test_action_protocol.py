"""Offline transport/accounting tests. All inference is replaced by local fixtures."""
import copy
import hashlib
import io
import json
import uuid
from types import SimpleNamespace

import pytest
from PIL import Image
from jsonschema import Draft202012Validator, FormatChecker

from ai_budget.models import AIUsageReservation
from ai_drafts import codex_provider
from ai_drafts.providers import ProviderFailure
from ai_drafts.validation import InvalidModelOutput
from media_pipeline.exceptions import ProcessingFailure
from media_pipeline.worker import JobContext, process_one
from visual_drafts import provider, worker
from visual_drafts.models import VisualAnalysisWindow, VisualProposal
from .test_visual_full_video import full_video, queue, fake_extraction
from .test_codex_provider import codex_config, cli_result


def images(count=12):
    output = io.BytesIO()
    Image.new('RGB', (960, 540), 'blue').save(output, format='JPEG')
    raw = output.getvalue()
    return [{'id': str(uuid.uuid4()), 'sequence': i + 1, 'timestamp_ms': i * 1000,
             'bytes': raw, 'sha256': hashlib.sha256(raw).hexdigest()} for i in range(count)]


def action_value(frames):
    ids = [str(frame['id']) for frame in frames]
    return {'schema_version': '3', 'language': 'ru', 'observations': [{
        'id': 'o1', 'kind': 'action', 'subject': 'Материал', 'title': 'Переместите материал',
        'before': None, 'before_frame_id': None,
        'movement': 'Сдвиньте материал рукой вдоль поверхности.', 'action_frame_ids': ids,
        'after': None, 'after_frame_id': None, 'evidence_frame_ids': ids,
    }], 'gaps': [], 'warnings': []}


def instance(backend='openai', protocol='3'):
    result = provider.VisualProvider.__new__(provider.VisualProvider)
    result.provider = backend
    result.protocol = protocol
    result.model = 'gpt-5.4-mini' if backend == 'openai' else 'gpt-6-luna'
    result.endpoint = 'https://api.openai.com/v1'
    return result


@pytest.mark.parametrize('mode', ['upload', 'live'])
def test_same_bound_contract_and_bytes_for_both_transports(monkeypatch, mode):
    source = images()
    expected = action_value(source)
    captured = {}

    def http(url, body, **kwargs):
        captured['http'] = copy.deepcopy(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(expected)}}]}

    def cli(**kwargs):
        captured['cli'] = kwargs
        return expected

    monkeypatch.setattr(provider, 'json_request', http)
    monkeypatch.setattr(provider, 'provider_api_key', lambda *_: '')
    monkeypatch.setattr(codex_provider, 'generate', cli)
    for backend in ('openai', 'codex'):
        assert instance(backend).generate(language='ru', mode=mode, frames=source, context=object()) == expected
    body, cli_request = captured['http'], captured['cli']
    assert body['messages'][0]['content'] == cli_request['system']
    assert body['response_format']['json_schema']['schema'] == cli_request['schema']
    assert body['max_completion_tokens'] == 8192
    assert cli_request['max_output_bytes'] == 32768
    assert cli_request['reasoning_effort'] == ('medium' if mode == 'upload' else 'low')
    assert cli_request['frames'] is source
    assert len([item for item in body['messages'][1]['content'] if item['type'] == 'image_url']) == 12
    validator = Draft202012Validator(cli_request['schema'], format_checker=FormatChecker())
    assert validator.is_valid(expected)
    bad = copy.deepcopy(expected)
    bad['observations'][0]['before'] = 'Другое состояние'
    bad['observations'][0]['before_frame_id'] = str(uuid.uuid4())
    assert not validator.is_valid(bad)
    assert not validator.is_valid({**expected, 'language': 'kk'})


def test_bound_contract_does_not_keep_previous_ids_or_mutate_schema():
    from visual_drafts.action_evidence import SCHEMA
    original = copy.deepcopy(SCHEMA)
    first, second = images(), images()
    _, first_schema = provider.request_contract('3', 'ru', 'upload', first)
    _, second_schema = provider.request_contract('3', 'kk', 'upload', second)
    assert SCHEMA == original
    assert first[0]['id'] in json.dumps(first_schema)
    assert first[0]['id'] not in json.dumps(second_schema)
    assert second[0]['id'] in json.dumps(second_schema)


@pytest.mark.parametrize('backend', ['openai', 'codex'])
def test_maximum_frame_request_fits_same_input_ceiling(backend):
    system, schema, request, body = instance(backend).prepare_request(language='mixed', mode='upload', frames=images())
    # prepare_request enforces the complete HTTP envelope, or the CLI schema +
    # prompt envelope including a conservative fixed wrapper allowance.
    assert len(json.dumps({'system': system, 'schema': schema, 'request': request}, ensure_ascii=False).encode()) + 1024 < 32768
    assert (body is None) == (backend == 'codex')


@pytest.mark.parametrize('backend', ['openai', 'codex'])
def test_overgrown_policy_never_reaches_transport(monkeypatch, backend):
    monkeypatch.setattr(provider, 'ACTION_SYSTEM', 'я' * 32768)
    monkeypatch.setattr(provider, 'json_request', lambda *a, **k: pytest.fail('HTTP dispatched'))
    monkeypatch.setattr(codex_provider, 'generate', lambda **k: pytest.fail('CLI dispatched'))
    with pytest.raises(ProviderFailure, match='AI_BUDGET_INPUT_UNBOUNDED'):
        instance(backend).generate(language='ru', mode='upload', frames=images())


def test_legacy_request_contract_remains_legacy_without_runtime_flag(settings):
    settings.VISUAL_ACTION_EVIDENCE = True
    item = instance(protocol='2')
    system, schema, _, _ = item.prepare_request(language='ru', mode='live', frames=images())
    assert system == provider.SYSTEM
    assert schema['properties']['schema_version']['enum'] == ['2']
    assert 'observations' not in schema['properties']


@pytest.mark.parametrize('job_version,session_version', [('unknown', 'unknown'), ('3', '2'), ('2', '3'), (None, '3')])
def test_mismatched_or_unknown_pins_fail_closed(job_version, session_version):
    payload = {} if job_version is None else {'visual_protocol': job_version}
    session = SimpleNamespace(analysis_budget={'visual_protocol': session_version})
    with pytest.raises(ProcessingFailure) as failure:
        worker.protocol_for_job(SimpleNamespace(payload=payload), session)
    assert failure.value.code == 'VISUAL_PROTOCOL_INVALID'


def test_validator_requires_expected_version():
    value = action_value(images(2))
    with pytest.raises(InvalidModelOutput, match='VISUAL_OUTPUT_INVALID'):
        provider.validate(value, [], SimpleNamespace())
    with pytest.raises(InvalidModelOutput, match='VISUAL_OUTPUT_INVALID'):
        provider.validate({'schema_version': '2'}, [], SimpleNamespace(), expected_version='3')


def test_completed_checkpoint_cannot_change_contract():
    job = SimpleNamespace(payload={'visual_protocol': '3'})
    session = SimpleNamespace(analysis_budget={'visual_protocol': '3'})
    window = SimpleNamespace(state='completed', content={'schema_version': '2'})
    with pytest.raises(ProcessingFailure) as failure:
        worker.run_window(job, session, window, None, None)
    assert failure.value.code == 'VISUAL_PROTOCOL_INVALID'
    window.content['schema_version'] = '3'
    assert worker.run_window(job, session, window, None, None) is None


def test_http_v3_rejects_raw_whitespace_overflow_without_retry(monkeypatch):
    source = images(2)
    calls = []
    raw = json.dumps(action_value(source)) + ' ' * 32768

    def http(*args, **kwargs):
        calls.append(1)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': raw}}]}

    monkeypatch.setattr(provider, 'json_request', http)
    monkeypatch.setattr(provider, 'provider_api_key', lambda *_: '')
    with pytest.raises(ProviderFailure, match='MODEL_OUTPUT_INVALID'):
        instance().generate(language='ru', mode='upload', frames=source)
    assert calls == [1]


@pytest.mark.django_db
@pytest.mark.parametrize('boundary', ['file', 'event'])
def test_codex_v3_rejects_raw_overflow_at_both_boundaries(team, codex_config, boundary):
    source = images(2)
    value = action_value(source)
    raw = json.dumps(value) + ' ' * 32768
    calls = []

    def run(argv, **kwargs):
        calls.append(1)
        result = cli_result(argv, value, output=raw if boundary == 'file' else None)
        if boundary == 'event':
            events = [json.loads(line) for line in result.stdout.splitlines()]
            for event in events:
                if event['type'] == 'item.completed':
                    event['item']['text'] = raw
            result.stdout = '\n'.join(json.dumps(event) for event in events)
        return result

    item = provider.VisualProvider(team.org)
    item.protocol = '3'
    with pytest.raises(ProviderFailure, match='MODEL_OUTPUT_INVALID'):
        item.generate(language='ru', mode='upload', frames=source, context=SimpleNamespace(run=run))
    assert calls == [1]


@pytest.mark.django_db
def test_input_rejection_does_not_reserve_or_dispatch(team, full_video, settings, monkeypatch):
    settings.VISUAL_ACTION_EVIDENCE = True
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)

    class Rejected(provider.VisualProvider):
        def __init__(self, organization):
            self.provider, self.model, self.endpoint = 'openai', 'gpt-5.4-mini', 'https://api.openai.com/v1'

        def prepare_request(self, **kwargs):
            raise ProviderFailure('AI_BUDGET_INPUT_UNBOUNDED', 'Too large')

        def generate(self, **kwargs):
            pytest.fail('model dispatched')

    monkeypatch.setattr(worker, 'VisualProvider', Rejected)
    job, session = queue(team, full_video, max_ai_calls=3)
    assert session.analysis_budget['visual_protocol'] == '3'
    assert process_one()
    assert AIUsageReservation.objects.count() == 0
    assert not VisualAnalysisWindow.objects.filter(session=session, state='completed').exists()
    content = VisualProposal.objects.get(session=session).content
    assert content['steps'] == [] and content['schema_version'] == '3'
    assert content['observations'] == [] and content['gaps'] == []


@pytest.mark.django_db
def test_real_worker_uses_one_bounded_call_per_window_and_keeps_v3(team, full_video, settings, monkeypatch):
    settings.VISUAL_ACTION_EVIDENCE = True
    calls = []
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
    # Keep the fixture's receipt on pytest's SQLite transaction connection;
    # transport threading and late receipts have their own integration tests.
    monkeypatch.setattr(JobContext, 'call', lambda self, function, timeout: function())

    class LocalModel(provider.VisualProvider):
        def __init__(self, organization):
            self.provider, self.model, self.endpoint = 'openai', 'gpt-5.4-mini', 'https://api.openai.com/v1'

        def generate(self, *, language, mode, frames, context=None):
            assert self.protocol == '3'
            calls.append(frames[0]['timestamp_ms'])
            self._usage_recorder({'prompt_tokens': 1000, 'completion_tokens': 100}, f'fixture-{len(calls)}')
            return action_value(frames)

    monkeypatch.setattr(worker, 'VisualProvider', LocalModel)
    job, session = queue(team, full_video, max_ai_calls=3)
    settings.VISUAL_ACTION_EVIDENCE = False  # Existing session retains its protocol.
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done', job.error_code
    assert calls == [0, 30000, 60000]
    assert AIUsageReservation.objects.filter(state='settled').count() == 3
    content = VisualProposal.objects.get(session=session).content
    assert content['schema_version'] == '3' and len(content['observations']) == 3
    assert len({observation['id'] for observation in content['observations']}) == 3
    assert len(content['steps']) == 3
    assert not full_video.working.steps.exists()
