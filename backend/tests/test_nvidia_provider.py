"""Provider boundaries only: no network calls or real credentials."""
import base64
import io
import json
import uuid

import pytest
from PIL import Image

from ai_drafts import providers
from visual_drafts import provider as vision

pytestmark = pytest.mark.django_db
MODEL = 'nvidia/nemotron-3.5-super-vl-120b-a12b'


@pytest.fixture(autouse=True)
def configured_nvidia(team, settings):
    settings.STEP_DRAFT_BACKEND = 'nvidia'
    settings.STEP_DRAFT_ENDPOINT = providers.NVIDIA_ENDPOINT
    settings.STEP_DRAFT_MODEL = MODEL
    settings.NVIDIA_API_KEY = 'nvidia-test-credential'
    settings.STEP_DRAFT_API_KEY = 'openai-test-credential'
    settings.OPENAI_API_KEY = 'openai-environment-test-credential'
    settings.EXTERNAL_AI_ENABLED = True
    team.org.external_ai_enabled = True
    team.org.save(update_fields=['external_ai_enabled'])


@pytest.mark.parametrize('disabled', ['key', 'global', 'organization'])
def test_nvidia_requires_own_key_and_both_cloud_permissions(team, settings, disabled):
    assert providers.endpoint_configuration(team.org)['available']
    assert vision.capabilities(team.org)['available']
    if disabled == 'key':
        settings.NVIDIA_API_KEY = ''
    elif disabled == 'global':
        settings.EXTERNAL_AI_ENABLED = False
    else:
        team.org.external_ai_enabled = False
    config = providers.endpoint_configuration(team.org)
    assert not config['available'] and config['external']
    assert not vision.capabilities(team.org)['available']
    if disabled == 'key':
        assert 'NVIDIA_API_KEY' in config['reason']
    with pytest.raises(providers.ProviderFailure):
        providers.StepDraftProvider(team.org)
    with pytest.raises(providers.ProviderFailure):
        vision.VisualProvider(team.org)


@pytest.mark.parametrize('endpoint', [
    'https://api.openai.com/v1',
    'http://integrate.api.nvidia.com/v1',
    'http://127.0.0.1:12345/v1',
    'https://integrate.api.nvidia.com.example/v1',
    'https://integrate.api.nvidia.com:8443/v1',
    'https://integrate.api.nvidia.com/v2',
    'https://integrate.api.nvidia.com/v1?target=other',
    'https://account@integrate.api.nvidia.com/v1',
])
def test_nvidia_rejects_noncanonical_endpoint_before_provider_creation(team, settings, endpoint):
    settings.STEP_DRAFT_ENDPOINT = endpoint
    assert not providers.endpoint_configuration(team.org)['available']
    with pytest.raises(providers.ProviderFailure):
        providers.StepDraftProvider(team.org)
    with pytest.raises(providers.ProviderFailure):
        vision.VisualProvider(team.org)


def test_nvidia_trailing_slash_is_normalized_without_leaking_credentials(team, settings):
    settings.STEP_DRAFT_ENDPOINT += '/'
    config = providers.capabilities(team.org, probe=False)
    assert config['step_drafts']['available']
    assert config['step_drafts']['provider'] == 'nvidia'
    assert config['step_drafts']['model'] == MODEL
    output = json.dumps(config)
    assert 'nvidia-test-credential' not in output
    assert 'openai-test-credential' not in output
    assert 'integrate.api.nvidia.com' not in output


@pytest.mark.parametrize('backend', ['nvidia', 'openai'])
@pytest.mark.parametrize('kind', ['speech', 'vision'])
def test_cloud_payload_and_credentials_are_specific_to_provider(team, settings, monkeypatch, backend, kind):
    settings.STEP_DRAFT_BACKEND = backend
    if backend == 'openai':
        settings.STEP_DRAFT_ENDPOINT = 'https://api.openai.com/v1'
        settings.STEP_DRAFT_MODEL = 'gpt-5-mini'
    captured = {}
    candidate = {'schema_version': '2' if kind == 'vision' else '1', 'language': 'ru', 'steps': [], 'warnings': []}
    def response(url, body, **kwargs):
        captured.update(url=url, body=body, **kwargs)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(candidate)}}]}
    monkeypatch.setattr(vision if kind == 'vision' else providers, 'json_request', response)
    frames = []
    if kind == 'vision':
        for index, color in enumerate(['blue', 'red']):
            image = io.BytesIO()
            Image.new('RGB', (10, 10), color).save(image, format='JPEG')
            frames.append({'id': str(uuid.uuid4()), 'sequence': index + 1, 'timestamp_ms': index * 1000, 'bytes': image.getvalue()})
        value = vision.VisualProvider(team.org).generate(language='ru', mode='live', frames=frames)
    else:
        value = providers.StepDraftProvider(team.org).generate({'segments': [{'text': 'Проверьте деталь.'}]})
    assert value == candidate
    body = captured['body']
    assert body['response_format']['type'] == 'json_schema'
    assert body['response_format']['json_schema']['strict'] is True
    assert 'tools' not in body
    assert captured['url'] == settings.STEP_DRAFT_ENDPOINT + '/chat/completions'
    assert body['model'] == settings.STEP_DRAFT_MODEL
    if backend == 'nvidia':
        assert captured['api_key'] == 'nvidia-test-credential'
        assert body['max_tokens'] == 8192
        assert body['stream'] is False and body['reasoning_effort'] == 'none'
        assert 'max_completion_tokens' not in body and 'store' not in body
    else:
        assert captured['api_key'] == 'openai-test-credential'
        assert body['max_completion_tokens'] == 8192 and body['store'] is False
        assert 'max_tokens' not in body and 'reasoning_effort' not in body
    if kind == 'vision':
        images = [item['image_url'] for item in body['messages'][1]['content'] if item['type'] == 'image_url']
        assert len(images) == len(frames)
        for image, frame in zip(images, frames):
            assert base64.b64decode(image['url'].split(',', 1)[1]) == frame['bytes']
            if backend == 'nvidia':
                assert set(image) == {'url'}
            else:
                assert image['detail'] == 'high'


def test_openai_never_falls_back_to_nvidia_key(team, settings):
    settings.STEP_DRAFT_BACKEND = 'openai'
    settings.STEP_DRAFT_ENDPOINT = 'https://api.openai.com/v1'
    settings.STEP_DRAFT_MODEL = 'gpt-5-mini'
    settings.STEP_DRAFT_API_KEY = ''
    assert not providers.endpoint_configuration(team.org)['available']


def test_pending_provider_response_is_not_retried_or_treated_as_a_proposal(monkeypatch):
    calls = []
    class PendingResponse:
        status = 202
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            raise AssertionError('Pending response is not a completed model result')
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return PendingResponse()
    monkeypatch.setattr(providers, 'build_opener', lambda *handlers: Opener())
    with pytest.raises(providers.ProviderFailure) as failed:
        providers.json_request(providers.NVIDIA_ENDPOINT + '/chat/completions', {})
    assert failed.value.code == 'AI_RESPONSE_PENDING' and not failed.value.retryable
    assert len(calls) == 1
