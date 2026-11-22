"""Exercise the kitchen release through its public commands, API and job worker."""
import json
from io import StringIO
from unittest.mock import Mock

import pytest
from django.core.management import call_command, CommandError
from rest_framework.test import APIClient

from ai_budget.models import AIUsageReservation
from ai_drafts import providers
from ai_drafts.models import Transcript, TranscriptSegment
from core.management.commands import kitchen_member, seed_demo
from core.models import Instruction, Membership, Organization, PublishedVersion, User
from media_pipeline.models import ProcessingJob
from media_pipeline.worker import JobContext, process_one
from visual_drafts import provider, worker
from visual_drafts.models import VisualSession
from .conftest import base, post
from .test_visual_drafts import add_frames, session_url
from .test_visual_full_video import fake_extraction


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('recipe', [True, False], ids=['recipe', 'legacy'])
@pytest.mark.parametrize('route', ['video', 'camera', 'transcript'])
def test_worker_routes_culinary_guidance_with_one_zero_price_call(team, settings, monkeypatch, recipe, route):
    model = 'dots-studio/dots-3-note-preview:free'
    settings.STEP_DRAFT_BACKEND = 'openrouter'
    settings.STEP_DRAFT_ENDPOINT = providers.OPENROUTER_ENDPOINT
    settings.STEP_DRAFT_MODEL = model
    settings.OPENROUTER_API_KEY = 'offline-test-only'
    settings.OPENROUTER_TOTAL_BUDGET_MICROUSD = 0
    settings.EXTERNAL_AI_ENABLED = True
    settings.VISUAL_ACTION_EVIDENCE = True
    settings.VISUAL_SHORT_ACTION_COVERAGE = False
    settings.VISUAL_SEMANTIC_REVIEW = False
    settings.VISUAL_DEMONSTRATION_PAIR_REVIEW = False
    team.org.external_ai_enabled = True
    team.org.save(update_fields=['external_ai_enabled'])
    client = team.clients['author']
    form = {'title': 'Овощная нарезка', 'purpose': 'Подготовим овощи для салата.',
            'language': 'ru', 'demo_source': True}
    if recipe:
        form['recipe'] = {'ingredients': [], 'servings': None}
    created = post(client, base(team), form, key='create')
    assert created.status_code == 201, created.data
    working = created.json()
    url = base(team) + '/' + working['instruction_id']

    if route == 'transcript':
        transcript = Transcript.objects.create(organization=team.org, asset_id=working['source']['id'],
                                               revision=1, duration_ms=60000, language='ru',
                                               provider='fixture', model='fixture', input_sha256='a' * 64)
        TranscriptSegment.objects.create(organization=team.org, transcript=transcript, ordinal=1,
                                         start_ms=0, end_ms=3000, raw_text='Нарежьте перец.', text='Нарежьте перец.')
        response = post(client, url + '/draft-proposals', {'transcript_id': str(transcript.pk),
                        'transcript_revision': 1, 'base_working_revision': working['revision']},
                        etag=working['etag'], key='transcript')
    elif route == 'camera':
        response = post(client, url + '/visual-sessions', {'consent_images': True}, etag=working['etag'], key='camera')
        assert response.status_code == 201, response.data
        session = VisualSession.objects.get(pk=response.json()['id'])
        add_frames(session)
        response = post(client, session_url(team, session) + '/analyze', key='frames')
    else:
        monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
        response = post(client, url + '/visual-proposals', {'consent_images': True, 'scope': 'window',
                        'start_ms': 0, 'end_ms': 3000, 'max_ai_calls': 1, 'max_cost_microusd': 0},
                        etag=working['etag'], key='video')
    assert response.status_code == 202, response.data
    if route != 'transcript':
        assert VisualSession.objects.get().analysis_budget.get('culinary_guidance') == ('v1' if recipe else None)

    requests = []
    def transport(endpoint, body, **kwargs):
        requests.append(body)
        generated = ({'schema_version': '1', 'language': 'ru', 'steps': [], 'warnings': []}
                     if route == 'transcript' else
                     {'schema_version': '3', 'language': 'ru', 'observations': [], 'gaps': [], 'warnings': []})
        return {'id': 'offline-receipt', 'model': model, 'usage': {'cost': 0, 'prompt_tokens': 10, 'completion_tokens': 10},
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(generated)}}]}

    monkeypatch.setattr(provider, 'json_request', transport)
    monkeypatch.setattr(providers, 'json_request', transport)
    # Run the transport and its usage receipt on the SQLite test transaction's connection.
    monkeypatch.setattr(JobContext, 'call', lambda self, operation, timeout: operation())
    assert process_one()
    job = ProcessingJob.objects.get(pk=response.json()['id'])
    assert job.state == 'done', (job.error_code, job.error_summary)
    assert len(requests) == 1
    body = requests[0]
    assert (providers.RECIPE_GUIDANCE in body['messages'][0]['content']) is recipe
    assert body['provider']['allow_fallbacks'] is False
    assert all(price == 0 for price in body['provider']['max_price'].values())
    receipt = AIUsageReservation.objects.get()
    assert (receipt.unit, receipt.state, receipt.actual_units, receipt.usage['cost_microusd']) == ('calls', 'settled', 1, 0)
    assert not PublishedVersion.objects.exists()


def test_private_kitchen_provisioning_is_repeatable_without_changing_an_existing_member(monkeypatch):
    password = 'only-for-this-test-Kitchen-41!'
    prompts = Mock(side_effect=[password, password])
    monkeypatch.setattr(kitchen_member, 'getpass', prompts)
    contact = dict(email=' Cook@example.test ', name='  Шеф  ', role='admin', stdout=StringIO())
    call_command('kitchen_member', new_kitchen='Тестовая кухня', **contact)
    org = Organization.objects.get()
    user = User.objects.get()
    assert (user.email, user.display_name) == ('cook@example.test', 'Шеф')
    assert user.check_password(password)
    call_command('kitchen_member', kitchen_id=org.pk, **contact)
    with pytest.raises(CommandError):
        call_command('kitchen_member', kitchen_id=org.pk, **{**contact, 'role': 'reader'})
    assert Membership.objects.get(user=user).role == 'admin'
    assert prompts.call_count == 2
    assert APIClient().login(email=user.email, password=password)
    assert not org.external_ai_enabled


def test_private_kitchen_creation_rolls_back_on_invalid_password(monkeypatch):
    monkeypatch.setattr(kitchen_member, 'getpass', lambda message: 'short')
    with pytest.raises(CommandError):
        call_command('kitchen_member', new_kitchen='Ещё кухня', email='chef@example.test', name='Новый шеф', role='admin')
    assert not Organization.objects.exists() and not User.objects.exists()


def test_demo_seed_keeps_existing_kitchen_accounts_and_published_recipe_and_serves_real_media(settings, team):
    settings.DEBUG = True
    org = Organization.objects.create(pk=seed_demo.DEMO_ORG, name='Моя учебная кухня')
    old_reader = User.objects.create_user(email='reader@demo.local', password='Preexisting-reader-password!', display_name='Наш читатель')
    Membership.objects.create(organization=org, user=old_reader, role='reader')
    call_command('seed_demo', password='Local-demo-password-for-tests!', stdout=StringIO())
    recipe = Instruction.objects.get(organization=org)
    version = recipe.current_version
    call_command('seed_demo', password='Another-valid-seed-password!', stdout=StringIO())
    assert Instruction.objects.filter(organization=org).count() == 1 and PublishedVersion.objects.count() == 1
    assert Membership.objects.filter(organization=org).count() == 4
    old_reader.refresh_from_db()
    assert old_reader.check_password('Preexisting-reader-password!') and old_reader.display_name == 'Наш читатель'
    assert Organization.objects.get(pk=org.pk).name == 'Моя учебная кухня'
    client = APIClient()
    client.force_login(old_reader)
    listing = '/api/v1/orgs/' + str(org.pk) + '/instructions'
    result = client.get(listing, {'kind': 'recipe', 'q': 'перец'}).json()['items']
    assert [item['id'] for item in result] == [str(recipe.pk)]
    detail = client.get(listing + '/' + str(recipe.pk)).json()
    assert detail['publication']['id'] == str(version.pk) and detail['working_copy'] is None
    snapshot = detail['publication']['snapshot']
    assert snapshot['recipe']['ingredients'] and all(s['checked'] for s in snapshot['steps'])
    video_url = snapshot['source']['playback_url']
    media = client.get(video_url, HTTP_RANGE='bytes=0-31')
    assert media.status_code == 206 and media['Content-Range'].startswith('bytes 0-31/')
    header = b''.join(media.streaming_content)
    assert len(header) == 32 and header[4:8] == b'ftyp'
    photo = client.get(snapshot['source']['thumbnail_url'])
    assert photo.status_code == 200 and b''.join(photo.streaming_content).startswith(b'\xff\xd8')
    assert team.outsider.get(video_url).status_code == 404
    assert APIClient().get(video_url).status_code == 401
    assert not AIUsageReservation.objects.exists()


def test_demo_seed_refuses_production(settings):
    settings.DEBUG = False
    with pytest.raises(CommandError):
        call_command('seed_demo', password='Local-demo-password-for-tests!')
    assert not Organization.objects.exists()
