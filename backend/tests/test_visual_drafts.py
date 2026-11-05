import copy
import hashlib
import io
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from PIL import Image
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from ai_drafts.validation import InvalidModelOutput
from core.models import Instruction, Membership, PublishedVersion
from media_pipeline.models import ProcessingJob
from media_pipeline.worker import process_one
from visual_drafts import provider, services, worker
from visual_drafts.cleanup import cleanup_visual_sessions
from visual_drafts.models import FrameEvidence, VisualProposal, VisualSession
from .conftest import document, post, save
from .test_video_frames import video_asset

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def configured_vision(settings, monkeypatch):
    settings.STEP_DRAFT_BACKEND = 'openai'
    settings.STEP_DRAFT_ENDPOINT = 'http://127.0.0.1:12345/v1'
    settings.STEP_DRAFT_MODEL = 'gpt-5.4-mini'
    settings.STEP_DRAFT_API_KEY = ''
    settings.EXTERNAL_AI_ENABLED = False
    # These tests exercise evidence/authoring contracts. Budget enforcement has
    # separate integration/concurrency tests with actual reservation models.
    monkeypatch.setattr(worker, 'execute_budgeted', lambda provider, **kwargs: kwargs['context'].call(lambda: provider.generate(language=kwargs['session'].language, mode=kwargs['session'].mode, frames=kwargs['frames']), timeout=100))


def jpeg(color='red', size=(80, 60), exif=False):
    image = Image.new('RGB', size, color)
    output = io.BytesIO()
    metadata = Image.Exif()
    if exif:
        metadata[0x010E] = 'private-camera-metadata'
    image.save(output, format='JPEG', exif=metadata)
    return output.getvalue()


def session_url(team, session):
    return f'/api/v1/orgs/{team.org.id}/visual-sessions/{session.id}'


def send_frame(team, session, raw, sequence=1, timestamp=0, key='frame-1', client=None):
    return (client or team.clients['author']).post(session_url(team, session) + '/frames', raw, content_type='image/jpeg', HTTP_X_FRAME_SEQUENCE=str(sequence), HTTP_X_TIMESTAMP_MS=str(timestamp), HTTP_IDEMPOTENCY_KEY=key)


def create_session(team, draft, mode='live', **extra):
    instruction = Instruction.objects.get(pk=draft.working['instruction_id'])
    return VisualSession.objects.create(organization=team.org, instruction=instruction, asset=instruction.source_asset if mode == 'upload' else None, created_by=team.users['author'], mode=mode, consent_images=True, base_working_revision=draft.working['revision'], language='ru', expires_at=timezone.now() + timedelta(minutes=10), window_start_ms=0, window_end_ms=60000 if mode == 'upload' else 600000, **extra)


def add_frames(session, count=2):
    frames = []
    for index in range(count):
        raw = jpeg('red' if index % 2 else 'blue')
        frames.append(services.persist_frame(session, sequence=index + 1, timestamp_ms=index * 2000, coverage_start_ms=index * 2000, coverage_end_ms=(index + 1) * 2000, content=raw, width=80, height=60, idempotency_key=f'frame-{index}', request_sha256=hashlib.sha256(raw).hexdigest()))
    return frames


def model_value(frames):
    return {'schema_version': '2', 'language': 'ru', 'steps': [{'title': 'Переместите деталь', 'description': 'Положение детали изменилось между показанными кадрами; проверьте действие по записи.', 'evidence_frame_ids': [str(frame.id) for frame in frames]}], 'warnings': []}


def make_proposal(team, draft, mode='upload'):
    session = create_session(team, draft, mode)
    frames = add_frames(session)
    content = provider.validate(model_value(frames), frames, session)
    proposal = VisualProposal.objects.create(organization=team.org, session=session, source_job_id=uuid.uuid4(), provider='test', model='fixture', content=content)
    if mode == 'upload':
        session.state = 'completed'
        session.save()
    return session, proposal, frames


def job_for(team, session, frames):
    return ProcessingJob.objects.create(organization=team.org, instruction=session.instruction, asset=session.asset, requested_by=team.users['author'], kind='analyze_live' if session.mode == 'live' else 'analyze_video', visual_session_id=session.id, payload={'visual_session_id': str(session.id), 'frame_ids': [str(frame.id) for frame in frames]})


def fake_provider(monkeypatch, *, callback=None):
    class Fake:
        provider, model = 'test', 'vision-fixture'
        def __init__(self, organization):
            pass
        def generate(self, *, language, mode, frames):
            assert 2 <= len(frames) <= 12
            assert all(frame['bytes'].startswith(b'\xff\xd8') for frame in frames)
            if callback:
                callback()
            return model_value([SimpleNamespace(id=frame['id']) for frame in frames])
    monkeypatch.setattr(worker, 'VisualProvider', Fake)


def test_live_start_requires_consent_and_ifmatch_but_not_a_video(team, draft):
    response = post(team.clients['author'], draft.url + '/visual-sessions', {'consent_images': False}, etag=draft.working['etag'], key='no-consent')
    assert response.status_code == 400
    response = post(team.clients['author'], draft.url + '/visual-sessions', {'consent_images': True}, key='no-etag')
    assert response.status_code == 428
    response = post(team.clients['author'], draft.url + '/visual-sessions', {'consent_images': True}, etag=draft.working['etag'], key='start')
    assert response.status_code == 201, response.json()
    assert response.json()['asset_id'] is None and response.json()['mode'] == 'live'
    duplicate = post(team.clients['author'], draft.url + '/visual-sessions', {'consent_images': True}, etag=draft.working['etag'], key='start')
    assert duplicate.json() == response.json()
    assert VisualSession.objects.count() == 1


def test_real_jpeg_round_trip_strips_metadata_private_read_and_idempotency(team, draft):
    session = create_session(team, draft)
    raw = jpeg(size=(1200, 800), exif=True)
    response = send_frame(team, session, raw)
    assert response.status_code == 201, response.json()
    frame = FrameEvidence.objects.get()
    assert frame.width == 960 and frame.height == 640
    path = services.safe_frame_path(frame.storage_key)
    assert path.stat().st_mode & 0o777 == 0o600
    with Image.open(path) as image:
        assert not image.getexif()
    assert send_frame(team, session, raw).status_code == 200
    assert FrameEvidence.objects.count() == 1
    assert send_frame(team, session, jpeg('green')).status_code == 409
    image_url = response.json()['url']
    read = team.clients['author'].get(image_url)
    assert read.status_code == 200 and read['Content-Type'] == 'image/jpeg'
    assert b''.join(read.streaming_content).startswith(b'\xff\xd8')
    assert team.clients['reader'].get(image_url).status_code == 403
    assert team.outsider.get(image_url).status_code == 404
    assert team.outsider.get(image_url.replace(str(team.org.id), str(team.other.id))).status_code == 404


@pytest.mark.parametrize('kind', ['huge', 'not_jpeg', 'png'])
def test_invalid_frames_never_reach_storage(team, draft, kind):
    session = create_session(team, draft)
    raw = b'x' * 524289 if kind == 'huge' else b'not a JPEG'
    if kind == 'png':
        output = io.BytesIO()
        Image.new('RGB', (10, 10)).save(output, format='PNG')
        raw = output.getvalue()
    response = send_frame(team, session, raw)
    assert response.status_code in {413, 422}
    assert not FrameEvidence.objects.exists()


def test_binary_camera_upload_enforces_csrf_without_parsing_body(team, draft):
    session = create_session(team, draft)
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(team.users['author'])
    response = send_frame(team, session, jpeg(), client=client)
    assert response.status_code == 403 and response.json()['error']['code'] == 'CSRF_FAILED'


def test_binary_camera_upload_with_real_csrf_cookie_and_header_succeeds(team, draft):
    session = create_session(team, draft)
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(team.users['author'])
    csrf = client.get('/api/v1/auth/csrf')
    assert csrf.status_code == 200 and 'csrftoken' in client.cookies
    response = client.post(session_url(team, session) + '/frames', jpeg(),
        content_type='image/jpeg', HTTP_X_CSRFTOKEN=csrf.json()['csrf_token'],
        HTTP_X_FRAME_SEQUENCE='1', HTTP_X_TIMESTAMP_MS='0', HTTP_IDEMPOTENCY_KEY='camera-csrf')
    assert response.status_code == 201, response.json()
    assert FrameEvidence.objects.get().session_id == session.id


def test_frame_owner_rate_order_and_timestamp_are_enforced(team, draft):
    session = create_session(team, draft)
    assert send_frame(team, session, jpeg(), client=team.clients['reviewer']).status_code == 403
    assert send_frame(team, session, jpeg(), sequence=2).status_code == 409
    assert send_frame(team, session, jpeg(), timestamp=10000).status_code == 422
    assert send_frame(team, session, jpeg()).status_code == 201
    response = send_frame(team, session, jpeg(), sequence=2, timestamp=2000, key='second')
    assert response.status_code == 429


def test_expired_or_cancelled_sessions_reject_frames_and_cancel_jobs(team, draft):
    session = create_session(team, draft)
    frames = add_frames(session)
    job = job_for(team, session, frames)
    response = post(team.clients['author'], session_url(team, session) + '/cancel', {})
    assert response.status_code == 200 and response.json()['state'] == 'cancelled'
    job.refresh_from_db()
    assert job.state == 'cancelled' and job.cancel_requested
    assert send_frame(team, session, jpeg(), sequence=3, timestamp=4000, key='late').status_code == 409
    another = create_session(team, draft)
    VisualSession.objects.filter(pk=another.id).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert send_frame(team, another, jpeg()).status_code == 409


@pytest.mark.parametrize('mutation', ['foreign', 'single', 'reverse', 'time', 'extra', 'duplicate'])
def test_visual_model_cannot_forge_sources_timing_or_commands(team, draft, mutation):
    session = create_session(team, draft, 'upload')
    frames = add_frames(session)
    value = model_value(frames)
    if mutation == 'foreign':
        value['steps'][0]['evidence_frame_ids'][0] = str(uuid.uuid4())
    elif mutation == 'single':
        value['steps'][0]['evidence_frame_ids'].pop()
    elif mutation == 'reverse':
        value['steps'][0]['evidence_frame_ids'].reverse()
    elif mutation == 'time':
        value['steps'][0]['start_ms'] = 0
    elif mutation == 'extra':
        value['publish'] = True
    elif mutation == 'duplicate':
        value['steps'][0]['evidence_frame_ids'][1] = value['steps'][0]['evidence_frame_ids'][0]
    with pytest.raises(InvalidModelOutput):
        provider.validate(value, frames, session)


def test_visual_time_is_server_derived_and_warnings_always_disclose_sampling(team, draft):
    session = create_session(team, draft, 'upload')
    frames = add_frames(session)
    content = provider.validate(model_value(frames), frames, session)
    assert content['steps'][0]['start_ms'] == 0 and content['steps'][0]['end_ms'] == 4000
    assert {'sampled_frames', 'approximate_timing'}.issubset({w['code'] for w in content['warnings']})


def test_provider_sends_ordered_real_image_bytes_no_public_urls_or_tools(team, monkeypatch):
    captured = {}
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    frames = [{'id': value, 'sequence': index + 1, 'timestamp_ms': index * 1000, 'bytes': jpeg()} for index, value in enumerate(ids)]
    def response(url, body, **kwargs):
        captured.update(body)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(model_value([SimpleNamespace(id=value) for value in ids]))}}]}
    monkeypatch.setattr(provider, 'json_request', response)
    result = provider.VisualProvider(team.org).generate(language='ru', mode='live', frames=frames)
    assert result['schema_version'] == '2'
    assert captured['store'] is False and 'tools' not in captured
    images = [item['image_url']['url'] for item in captured['messages'][1]['content'] if item['type'] == 'image_url']
    assert len(images) == 2 and all(value.startswith('data:image/jpeg;base64,/9j/') for value in images)
    assert 'недоверенные' in captured['messages'][0]['content']
    schema = captured['response_format']['json_schema']['schema']
    assert schema['properties']['schema_version'] == {'type': 'string', 'enum': ['2']}
    assert schema['properties']['language']['type'] == 'string'
    assert schema['properties']['warnings']['items']['properties']['code']['type'] == 'string'


@pytest.mark.parametrize('backend', ['openai', 'nvidia', 'codex'])
def test_provider_schema_rejects_foreign_frame_and_wrong_language_before_generation(team, monkeypatch, backend):
    from jsonschema import Draft202012Validator
    from ai_drafts import codex_provider
    captured = {}
    original_schema = copy.deepcopy(provider.SCHEMA)
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    frames = [{'id': value, 'sequence': index + 1, 'timestamp_ms': index * 1000, 'bytes': jpeg()} for index, value in enumerate(ids)]
    value = model_value([SimpleNamespace(id=value) for value in ids])
    monkeypatch.setattr(provider, 'capabilities', lambda org: {'available': True, 'provider': backend, 'model': 'test-model'})
    def api_response(url, body, **kwargs):
        captured['schema'] = body['response_format']['json_schema']['schema']
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]}
    def codex_response(**kwargs):
        captured['schema'] = kwargs['schema']
        return value
    monkeypatch.setattr(provider, 'json_request', api_response)
    monkeypatch.setattr(codex_provider, 'generate', codex_response)
    provider.VisualProvider(team.org).generate(language='ru', mode='live', frames=frames)
    validator = Draft202012Validator(captured['schema'])
    assert validator.is_valid(value)
    wrong_language = {**value, 'language': 'kk'}
    assert not validator.is_valid(wrong_language)
    foreign = copy.deepcopy(value)
    foreign['steps'][0]['evidence_frame_ids'][0] = str(uuid.uuid4())
    assert not validator.is_valid(foreign)
    # A later call must not inherit allowed IDs from the first request.
    next_frames = [{**frame, 'id': str(uuid.uuid4())} for frame in frames]
    provider.VisualProvider(team.org).generate(language='kk', mode='upload', frames=next_frames)
    next_value = model_value([SimpleNamespace(id=frame['id']) for frame in next_frames])
    next_value['language'] = 'kk'
    assert Draft202012Validator(captured['schema']).is_valid(next_value)
    assert not Draft202012Validator(captured['schema']).is_valid({**value, 'language': 'kk'})
    assert provider.SCHEMA == original_schema


@pytest.mark.parametrize('mutation,code', [
    ('language', 'VISUAL_LANGUAGE_MISMATCH'),
    ('foreign', 'VISUAL_EVIDENCE_UNKNOWN'),
    ('gap', 'VISUAL_EVIDENCE_ORDER_INVALID'),
    ('reverse', 'VISUAL_EVIDENCE_ORDER_INVALID'),
    ('duplicate', 'VISUAL_OUTPUT_INVALID'),
])
def test_bad_live_evidence_has_safe_diagnostic_and_never_changes_working_copy(team, draft, monkeypatch, mutation, code):
    session = create_session(team, draft)
    frames = add_frames(session, count=3)
    before = list(session.instruction.working_copy.steps.values())
    value = model_value(frames)
    value['steps'][0]['description'] = 'PRIVATE_MODEL_TEXT_MUST_NOT_REACH_ERROR'
    if mutation == 'language':
        value['language'] = 'kk'
    elif mutation == 'foreign':
        value['steps'][0]['evidence_frame_ids'][0] = str(uuid.uuid4())
    elif mutation == 'gap':
        value['steps'][0]['evidence_frame_ids'].pop(1)
    elif mutation == 'reverse':
        value['steps'][0]['evidence_frame_ids'].reverse()
    else:
        value['steps'][0]['evidence_frame_ids'][1] = value['steps'][0]['evidence_frame_ids'][0]
    class Fake:
        provider, model = 'test', 'fixture'
        def __init__(self, organization):
            pass
        def generate(self, **kwargs):
            return value
    monkeypatch.setattr(worker, 'VisualProvider', Fake)
    job = job_for(team, session, frames)
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'failed' and job.error_code == code
    assert 'PRIVATE_MODEL_TEXT' not in job.error_summary
    assert all(str(frame.id) not in job.error_summary for frame in frames)
    assert not VisualProposal.objects.exists()
    assert list(session.instruction.working_copy.steps.values()) == before
    assert not PublishedVersion.objects.exists()


def test_live_analysis_is_a_durable_preliminary_observation_never_apply(team, draft, monkeypatch):
    session = create_session(team, draft)
    frames = add_frames(session)
    fake_provider(monkeypatch)
    before = list(session.instruction.working_copy.steps.values())
    response = post(team.clients['author'], session_url(team, session) + '/analyze', {}, key='analyze')
    assert response.status_code == 202, response.json()
    assert response.json()['asset_id'] is None
    assert process_one()
    job = ProcessingJob.objects.get(pk=response.json()['id'])
    assert job.state == 'done', (job.error_code, job.error_summary)
    proposal = VisualProposal.objects.get()
    result = team.clients['author'].get(session_url(team, session)).json()
    assert result['observations'][0]['preliminary'] and not result['observations'][0]['can_apply']
    assert len(result['observations'][0]['frames']) == 2
    apply = post(team.clients['author'], draft.url + f'/visual-proposals/{proposal.id}/apply', {'confirm_replace': True}, etag=draft.working['etag'], key='forbidden-apply')
    assert apply.status_code == 409 and apply.json()['error']['code'] == 'LIVE_PROPOSAL_READ_ONLY'
    assert list(session.instruction.working_copy.steps.values()) == before
    assert not PublishedVersion.objects.exists()


@pytest.mark.parametrize('revoke', ['user', 'consent_policy', 'session', 'expiry'])
def test_revoked_live_job_never_calls_cloud(team, draft, monkeypatch, settings, revoke):
    session = create_session(team, draft)
    frames = add_frames(session)
    job = job_for(team, session, frames)
    if revoke == 'user':
        team.users['author'].is_active = False
        team.users['author'].save()
    elif revoke == 'consent_policy':
        settings.STEP_DRAFT_ENDPOINT = 'https://api.openai.com/v1'
        settings.STEP_DRAFT_API_KEY = 'test-key'
        settings.EXTERNAL_AI_ENABLED = False
    elif revoke == 'session':
        VisualSession.objects.filter(pk=session.id).update(state='cancelled')
    else:
        VisualSession.objects.filter(pk=session.id).update(expires_at=timezone.now() - timedelta(seconds=1))
    called = []
    monkeypatch.setattr(worker, 'VisualProvider', lambda *args: called.append(True))
    assert process_one()
    job.refresh_from_db()
    assert job.state in {'failed', 'cancelled'}
    assert called == [] and not VisualProposal.objects.exists()


def test_cancel_between_provider_and_commit_discards_late_result(team, draft, monkeypatch):
    session = create_session(team, draft)
    frames = add_frames(session)
    job = job_for(team, session, frames)
    fake_provider(monkeypatch)
    from media_pipeline.worker import JobContext
    def cancelled_after_call(self, function, timeout):
        result = function()
        VisualSession.objects.filter(pk=session.id).update(state='cancelled')
        return result
    monkeypatch.setattr(JobContext, 'call', cancelled_after_call)
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'failed' and job.error_code == 'VISUAL_SESSION_CLOSED'
    assert not VisualProposal.objects.exists()


@pytest.mark.parametrize('mode', ['append', 'replace'])
def test_uploaded_apply_is_explicit_preserves_provenance_and_never_publishes(team, draft, mode):
    session, proposal, frames = make_proposal(team, draft)
    before = list(session.instruction.working_copy.steps.values_list('id', flat=True))
    url = draft.url + f'/visual-proposals/{proposal.id}/apply'
    response = post(team.clients['author'], url, {'confirm_replace': True, 'mode': mode}, etag=draft.working['etag'], key='apply')
    assert response.status_code == 200, response.json()
    working = response.json()
    assert len(working['steps']) == (3 if mode == 'append' else 1)
    if mode == 'append':
        assert [step['id'] for step in working['steps'][:2]] == [str(value) for value in before]
    step = working['steps'][-1]
    assert step['origin'] == 'ai' and not step['checked']
    assert step['evidence_frame_ids'] == [str(frame.id) for frame in frames]
    assert step['evidence_visual_session_id'] == str(session.id)
    assert not PublishedVersion.objects.exists()
    repeat = post(team.clients['author'], url, {'confirm_replace': True, 'mode': mode}, etag=draft.working['etag'], key='apply')
    assert repeat.json() == working
    payload = document(working)
    # Existing document() helper predates visual evidence: include it for edits.
    for current, data in zip(working['steps'], payload['steps']):
        data['evidence_frame_ids'] = current['evidence_frame_ids']
    payload['steps'][-1]['description'] = 'Проверенное автором описание видимого действия.'
    edited = save(team.clients['author'], draft.url, working, payload)
    assert edited.status_code == 200, edited.json()
    assert edited.json()['steps'][-1]['origin'] == 'ai_edited'


def test_stale_visual_proposal_preserves_working_steps(team, draft):
    session, proposal, _ = make_proposal(team, draft)
    updated = save(team.clients['author'], draft.url, draft.working, document(draft.working, purpose='Изменённая цель после получения предложения')).json()
    before = list(session.instruction.working_copy.steps.values())
    response = post(team.clients['author'], draft.url + f'/visual-proposals/{proposal.id}/apply', {'confirm_replace': True, 'mode': 'append'}, etag=updated['etag'], key='stale')
    assert response.status_code == 409 and response.json()['error']['code'] == 'STALE_PROPOSAL'
    assert list(session.instruction.working_copy.steps.values()) == before


def test_no_audio_uploaded_video_queues(team, draft):
    instruction = Instruction.objects.get(pk=draft.working['instruction_id'])
    assert not instruction.source_asset.has_audio
    response = post(team.clients['author'], draft.url + '/visual-proposals', {'consent_images': True}, etag=draft.working['etag'], key='no-audio')
    assert response.status_code == 202, response.json()
    assert ProcessingJob.objects.get(pk=response.json()['id']).kind == 'analyze_video'


def test_long_video_defaults_to_complete_plan_and_preserves_explicit_window(team):
    from core.models import MediaAsset, WorkingCopy
    asset = MediaAsset.objects.create(organization=team.org, storage_key='fixture-long.mp4', duration_ms=120000, status='ready', has_audio=False)
    instruction = Instruction.objects.create(organization=team.org, owner=team.users['author'], source_asset=asset)
    working = WorkingCopy.objects.create(organization=team.org, instruction=instruction, title='Длинное видео', purpose='Проверить выбор нужного фрагмента', language='ru')
    from core.services import etag
    url = f'/api/v1/orgs/{team.org.id}/instructions/{instruction.id}/visual-proposals'
    missing = post(team.clients['author'], url, {'consent_images': True}, etag=etag(working), key='no-window')
    assert missing.status_code == 202, missing.json()
    planned = VisualSession.objects.get()
    assert planned.analysis_scope == 'full' and planned.analysis_windows.count() == 4
    assert missing.json()['result']['analysis']['windows_total'] == 4
    ProcessingJob.objects.filter(pk=missing.json()['id']).update(state='cancelled')
    excessive = post(team.clients['author'], url, {'consent_images': True, 'start_ms': 0, 'end_ms': 60001}, etag=etag(working), key='too-wide')
    assert excessive.status_code == 422
    valid = post(team.clients['author'], url, {'consent_images': True, 'start_ms': 60000, 'end_ms': 120000}, etag=etag(working), key='window')
    assert valid.status_code == 202, valid.json()
    session = VisualSession.objects.exclude(pk=planned.id).get()
    assert session.window_start_ms == 60000 and session.window_end_ms == 120000


@pytest.mark.parametrize('same_size', [False, True])
def test_changed_frame_bytes_fail_before_any_cloud_call(team, draft, monkeypatch, same_size):
    session = create_session(team, draft)
    frames = add_frames(session)
    job = job_for(team, session, frames)
    path = services.safe_frame_path(frames[0].storage_key)
    original = path.read_bytes()
    path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]) if same_size else b'changed frame')
    called = []
    monkeypatch.setattr(worker, 'VisualProvider', lambda *args: called.append(True))
    assert process_one()
    job.refresh_from_db()
    expected_code = 'VISUAL_FRAME_CHANGED' if same_size else 'VISUAL_FRAME_UNAVAILABLE'
    assert job.state == 'failed' and job.error_code == expected_code
    assert called == [] and not VisualProposal.objects.exists()


def test_frame_quota_failure_leaves_no_file_or_row(team, draft, monkeypatch, settings):
    session = create_session(team, draft)
    monkeypatch.setattr(services, 'FRAME_STORAGE_LIMIT', 100)
    response = send_frame(team, session, jpeg())
    assert response.status_code == 429
    assert not FrameEvidence.objects.exists()
    assert not list(settings.PRIVATE_MEDIA_ROOT.glob('visual-evidence/**/*.jpg'))


def test_live_call_and_frame_limits_are_enforced(team, draft):
    session = create_session(team, draft)
    add_frames(session)
    VisualSession.objects.filter(pk=session.id).update(analysis_calls=60)
    response = post(team.clients['author'], session_url(team, session) + '/analyze', {}, key='over-limit')
    assert response.status_code == 429 and response.json()['error']['code'] == 'VISUAL_CALL_LIMIT'
    assert not ProcessingJob.objects.exists()
    assert send_frame(team, session, jpeg(), sequence=301, timestamp=599999, key='over-frame-limit').status_code == 400


def test_visual_tenant_and_immutable_evidence_database_guards(team, draft):
    session, proposal, frames = make_proposal(team, draft)
    with pytest.raises(IntegrityError), transaction.atomic():
        FrameEvidence.objects.filter(pk=frames[0].id).update(timestamp_ms=10)
    with pytest.raises(IntegrityError), transaction.atomic():
        VisualProposal.objects.filter(pk=proposal.id).update(content={'publish': True})
    with pytest.raises(IntegrityError), transaction.atomic():
        VisualSession.objects.filter(pk=session.id).update(organization=team.other)


def test_cleanup_purges_unused_live_but_keeps_applied_uploaded_evidence(team, draft, django_capture_on_commit_callbacks):
    live = create_session(team, draft)
    frames = add_frames(live)
    VisualSession.objects.filter(pk=live.id).update(state='cancelled', expires_at=timezone.now() - timedelta(hours=2))
    paths = [services.safe_frame_path(frame.storage_key) for frame in frames]
    uploaded, proposal, persisted = make_proposal(team, draft)
    VisualProposal.objects.filter(pk=proposal.id).update(applied_at=timezone.now())
    VisualSession.objects.filter(pk=uploaded.id).update(created_at=timezone.now() - timedelta(days=8))
    with django_capture_on_commit_callbacks(execute=True):
        result = cleanup_visual_sessions()
    assert result['purged'] == 1
    assert not any(path.exists() for path in paths)
    assert uploaded.frames.count() == 2 and all(services.safe_frame_path(frame.storage_key).is_file() for frame in persisted)


def test_real_video_decode_to_private_evidence_then_vision_job(video_asset, team, monkeypatch):
    asset, context = video_asset
    instruction = Instruction.objects.get(source_asset=asset)
    session = VisualSession.objects.create(organization=team.org, instruction=instruction, asset=asset, created_by=team.users['author'], mode='upload', consent_images=True, base_working_revision=instruction.working_copy.revision, language='ru', expires_at=timezone.now() + timedelta(minutes=10), window_start_ms=0, window_end_ms=2000)
    job = job_for(team, session, [])
    fake_provider(monkeypatch)
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done', (job.error_code, job.error_summary)
    session.refresh_from_db()
    assert session.state == 'completed' and session.frames.count() == 12
    assert session.source_manifest['sparse_sampling'] is True
    assert VisualProposal.objects.get().content['steps'][0]['end_ms'] == 2000
    assert not session.instruction.working_copy.steps.exists()
