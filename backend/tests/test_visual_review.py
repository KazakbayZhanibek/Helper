"""Local review transport/integrity checks, not a model-accuracy evaluation."""
from copy import deepcopy
import hashlib
from io import BytesIO
import json
from types import SimpleNamespace
import uuid

from PIL import Image
import pytest

from ai_budget.services import MAX_TEXT_BYTES
from ai_drafts import codex_provider
from ai_drafts.providers import ProviderFailure
from ai_drafts.validation import InvalidModelOutput
from visual_drafts import action_evidence, provider, review
from .test_action_evidence import frames, session, ids, payload


@pytest.fixture
def images(frames):
    stream = BytesIO()
    Image.new('RGB', (32, 32), '#112233').save(stream, 'JPEG')
    raw = stream.getvalue()
    return [{'id': str(frame.id), 'sequence': index + 1, 'timestamp_ms': index * 1000,
             'bytes': raw, 'sha256': hashlib.sha256(raw).hexdigest()} for index, frame in enumerate(frames)]


def configured(monkeypatch, candidate, backend='codex'):
    monkeypatch.setattr(provider, 'capabilities', lambda org: {'available': True, 'provider': backend, 'model': 'test-model'})
    return review.VisualReviewProvider(SimpleNamespace(), candidate=candidate)


def test_pure_review_contract_separates_untrusted_candidate_and_preserves_images(frames, images):
    value = payload(frames)
    snapshot = deepcopy(value)
    system, schema, request = review.request_review_contract('ru', 'upload', images, value)
    assert system == review.REVIEW_SYSTEM
    assert request['review_version'] == review.REVIEW_VERSION
    assert request['unverified_candidate'] == value
    assert request['frames'] == [{key: frame[key] for key in ('id', 'sequence', 'timestamp_ms')} for frame in images]
    assert all('bytes' not in item for item in request['frames'])
    assert schema['properties']['language']['enum'] == ['ru']
    assert schema['properties']['schema_version']['enum'] == ['3']
    request['unverified_candidate']['observations'][0]['movement'] = 'Changed'
    assert value == snapshot


def test_normalized_projection_strips_only_server_fields_without_mutating_content(frames, session):
    content = action_evidence.normalize(payload(frames), frames, session)
    content['gaps'] = [{'id': f'b:r{index}', 'reason': 'unclear_action', 'description': f'Вопрос {index}',
                        'evidence_frame_ids': ids(frames, 0, 2), 'start_ms': 0, 'end_ms': 2000} for index in range(10)]
    content['warnings'] = [{'code': 'uncertain_action', 'message': f'Вопрос {index}'} for index in range(32)]
    before = deepcopy(content)
    result = review.candidate_from_content(content)
    action_evidence._check_payload(result)
    assert set(result) == {'schema_version', 'language', 'observations', 'gaps', 'warnings'}
    assert result['observations'][0]['id'] == 'o1'
    assert 'start_ms' not in result['observations'][0]
    assert len(result['gaps']) == 8 and len(result['warnings']) == 30
    assert result['gaps'][0] == {key: content['gaps'][0][key] for key in ('reason', 'description', 'evidence_frame_ids')}
    assert content == before
    result['observations'][0]['evidence_frame_ids'].clear()
    assert content == before


@pytest.mark.parametrize('mutation', ['too_many', 'duplicate_id', 'missing_field', 'wrong_version', 'bad_gap'])
def test_projection_rejects_invalid_observations_instead_of_dropping_them(frames, session, mutation):
    content = action_evidence.normalize(payload(frames), frames, session)
    if mutation == 'too_many':
        content['observations'] = [{**content['observations'][0], 'id': f'b:o{i}'} for i in range(13)]
    elif mutation == 'duplicate_id':
        content['observations'] *= 2
    elif mutation == 'missing_field':
        content['observations'][0].pop('movement')
    elif mutation == 'wrong_version':
        content['schema_version'] = '2'
    else:
        content['gaps'] = [None]
    with pytest.raises(ProviderFailure, match='VISUAL_REVIEW_CANDIDATE_INVALID'):
        review.candidate_from_content(content)


def test_phase_invalid_candidate_is_preserved_for_review_not_silently_repaired(frames, images):
    value = payload(frames)
    value['observations'][0]['action_frame_ids'] = ids(frames, 1, 2)
    _, _, request = review.request_review_contract('ru', 'upload', images, value)
    assert request['unverified_candidate'] == value


@pytest.mark.parametrize('field', ['evidence_frame_ids', 'action_frame_ids', 'before_frame_id', 'after_frame_id', 'gap'])
def test_candidate_foreign_refs_reject_before_dispatch_even_with_bad_phase(frames, images, field):
    value = payload(frames)
    value['observations'][0]['before'] = None
    foreign = str(uuid.UUID(int=999))
    if field == 'gap':
        value['gaps'] = [{'reason': 'unclear_action', 'description': 'Неясно.', 'evidence_frame_ids': [str(frames[0].id), foreign]}]
    elif field.endswith('_ids'):
        value['observations'][0][field] = [foreign] if field == 'action_frame_ids' else [str(frames[0].id), foreign]
    else:
        value['observations'][0][field] = foreign
    with pytest.raises(ProviderFailure, match='VISUAL_EVIDENCE_UNKNOWN'):
        review.request_review_contract('ru', 'upload', images, value)


@pytest.mark.parametrize('mutation', ['order', 'duplicate', 'extra', 'duplicate_id', 'blank'])
def test_candidate_order_schema_and_duplicate_ids_are_not_repaired(frames, images, mutation):
    value = payload(frames)
    if mutation == 'order':
        value['observations'][0]['action_frame_ids'].reverse()
    elif mutation == 'duplicate':
        value['observations'][0]['action_frame_ids'] = ids(frames, 1, 2) * 2
    elif mutation == 'extra':
        value['steps'] = []
    elif mutation == 'duplicate_id':
        value['observations'] *= 2
    else:
        value['observations'][0]['after'] = ' '
    with pytest.raises(ProviderFailure):
        review.request_review_contract('ru', 'upload', images, value)


@pytest.mark.parametrize('mutation', ['duplicate', 'reverse', 'time', 'too_few', 'too_many', 'id'])
def test_frame_metadata_is_bounded_and_ordered_before_dispatch(frames, images, mutation):
    if mutation == 'duplicate':
        images[1]['id'] = images[0]['id']
    elif mutation == 'reverse':
        images.reverse()
    elif mutation == 'time':
        images[1]['timestamp_ms'] = 0
    elif mutation == 'too_few':
        images = images[:1]
    elif mutation == 'too_many':
        images *= 3
    else:
        images[0]['id'] = 'not-a-uuid'
    with pytest.raises(ProviderFailure, match='VISUAL_EVIDENCE_INVALID'):
        review.request_review_contract('ru', 'upload', images, payload(frames))


@pytest.mark.parametrize('mode,language,error', [('live', 'ru', 'VISUAL_REVIEW_MODE_INVALID'), ('upload', 'kk', 'VISUAL_LANGUAGE_MISMATCH')])
def test_review_is_upload_only_and_language_bound(frames, images, mode, language, error):
    with pytest.raises(ProviderFailure, match=error):
        review.request_review_contract(language, mode, images, payload(frames))


def test_review_request_cannot_exceed_bounded_text_even_when_candidate_wire_fits(frames, images):
    value = payload(frames)
    item = value['observations'][0]
    item.update(subject='s' * 120, title='t' * 120, before='b' * 240, movement='m' * 800, after='a' * 240)
    value['observations'] = [{**item, 'id': f'o{index + 1}'} for index in range(12)]
    action_evidence._check_payload(value)
    assert len(json.dumps(value).encode()) < MAX_TEXT_BYTES
    with pytest.raises(ProviderFailure, match='AI_BUDGET_INPUT_UNBOUNDED'):
        review.request_review_contract('ru', 'upload', images, value)


@pytest.mark.parametrize('backend', ['openai', 'nvidia', 'codex'])
def test_one_generate_sends_one_review_request_with_same_images_and_usage_bounds(frames, images, monkeypatch, backend):
    value = payload(frames)
    client = configured(monkeypatch, value, backend)
    value['observations'][0]['movement'] = 'Mutation after provider creation'
    sent, receipts = [], []
    response = payload(frames)

    def codex_response(**kwargs):
        sent.append(kwargs)
        return response

    def http_response(url, body, **kwargs):
        sent.append(body)
        return {'id': 'review-receipt', 'usage': {'input_tokens': 10, 'output_tokens': 20},
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(response)}}]}

    monkeypatch.setattr(codex_provider, 'generate', codex_response)
    monkeypatch.setattr(provider, 'json_request', http_response)
    client._usage_recorder = lambda usage, request_id: receipts.append((usage, request_id))
    prepared = client.prepare_request(language='ru', mode='upload', frames=images)
    assert sent == []
    result = client.generate(language='ru', mode='upload', frames=images, context=object())
    assert result == response and len(sent) == 1
    assert prepared[2]['unverified_candidate'] == response
    if backend == 'codex':
        assert sent[0]['frames'] == images
        assert sent[0]['system'] == review.REVIEW_SYSTEM
        assert sent[0]['request'] == prepared[2]
        assert sent[0]['max_output_bytes'] == 32768
        assert sent[0]['reasoning_effort'] == 'medium'
    else:
        body = sent[0]
        assert body['messages'][0]['content'] == review.REVIEW_SYSTEM
        assert json.loads(body['messages'][1]['content'][0]['text']) == prepared[2]
        urls = [item['image_url']['url'] for item in body['messages'][1]['content'] if item['type'] == 'image_url']
        assert len(urls) == len(images) and all(url.startswith('data:image/jpeg;base64,') for url in urls)
        assert receipts == [({'input_tokens': 10, 'output_tokens': 20}, 'review-receipt')]
        assert body.get('max_tokens', body.get('max_completion_tokens')) == 8192
        assert 'tools' not in body


def test_provider_failure_has_no_hidden_retry(frames, images, monkeypatch):
    calls = []
    client = configured(monkeypatch, payload(frames), 'openai')

    def failure(*args, **kwargs):
        calls.append(True)
        raise ProviderFailure('TIMEOUT', 'Ошибка.')

    monkeypatch.setattr(provider, 'json_request', failure)
    with pytest.raises(ProviderFailure, match='TIMEOUT'):
        client.generate(language='ru', mode='upload', frames=images)
    assert len(calls) == 1


def test_review_response_still_needs_normalization_and_rejects_foreign_frames(frames, images, session, monkeypatch):
    response = payload(frames)
    response['observations'][0]['after_frame_id'] = str(uuid.UUID(int=999))
    client = configured(monkeypatch, payload(frames))
    monkeypatch.setattr(codex_provider, 'generate', lambda **kwargs: response)
    result = client.generate(language='ru', mode='upload', frames=images, context=object())
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_UNKNOWN'):
        provider.validate(result, frames, session, expected_version='3')


def test_oversized_review_output_rejected_by_existing_http_bound(frames, images, monkeypatch):
    client = configured(monkeypatch, payload(frames), 'openai')
    monkeypatch.setattr(provider, 'json_request', lambda *args, **kwargs: {
        'choices': [{'finish_reason': 'stop', 'message': {'content': ' ' * 32769}}]})
    with pytest.raises(ProviderFailure, match='MODEL_OUTPUT_INVALID'):
        client.generate(language='ru', mode='upload', frames=images)
