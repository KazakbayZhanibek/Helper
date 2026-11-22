import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from billing.services import recipe_count
from core.models import AuditEvent, Instruction
from media_pipeline.models import ProcessingJob, UploadSession
from visual_drafts.models import VisualSession
from tests.conftest import base, post, save, document

pytestmark = pytest.mark.django_db


@pytest.fixture
def recipe(team):
    response = post(team.clients['author'], base(team), {
        'title': 'Омлет с зеленью', 'language': 'ru', 'recipe': {'ingredients': ['Яйца'], 'servings': 2},
    }, key=str(uuid.uuid4()))
    assert response.status_code == 201
    value = response.json()
    return base(team) + '/' + value['instruction_id'], value


@pytest.mark.parametrize('role', ['author', 'admin'])
def test_draft_removed_from_library_releases_quota_and_keeps_audit(team, recipe, role):
    url, working = recipe
    client = team.clients[role]
    assert client.get(url + '/working-copy').json()['can_delete_draft']
    assert recipe_count(team.org) == 1
    assert client.delete(url, HTTP_IF_MATCH=working['etag']).status_code == 204
    assert client.get(url).status_code == 404
    assert client.get(url + '/working-copy').status_code == 404
    assert client.get(base(team)).json()['total'] == 0 and recipe_count(team.org) == 0
    assert Instruction.objects.get(pk=working['instruction_id']).lifecycle == 'deleted'
    assert client.delete(url, HTTP_IF_MATCH=working['etag']).status_code == 404
    assert AuditEvent.objects.filter(organization=team.org, actor=team.users[role],
                                     action='recipe.draft_deleted', object_id=working['instruction_id']).count() == 1


def test_draft_deletion_requires_fresh_revision_and_owner(team, recipe):
    url, working = recipe
    client = team.clients['author']
    assert client.delete(url).status_code == 428
    newer = save(client, url, working, document(working, recipe=working['recipe'], title='Омлет со шпинатом'))
    assert newer.status_code == 200
    assert client.delete(url, HTTP_IF_MATCH=working['etag']).status_code == 412
    headers = {'HTTP_IF_MATCH': newer.json()['etag']}
    assert team.outsider.delete(url, **headers).status_code == 404
    for role in ('reader', 'reviewer'):
        assert team.clients[role].delete(url, **headers).status_code == 403
    Instruction.objects.filter(pk=working['instruction_id']).update(owner=team.users['reviewer'])
    assert not client.get(url + '/working-copy').json()['can_delete_draft']
    assert client.delete(url, **headers).status_code == 403
    assert recipe_count(team.org) == 1


@pytest.mark.parametrize('activity', ['pending_upload', 'recording', 'queued_job', 'running_job'])
def test_draft_deletion_waits_for_active_video_operation(team, recipe, activity):
    url, working = recipe
    common = {'organization': team.org, 'instruction_id': working['instruction_id']}
    author = team.users['author']
    expiry = timezone.now() + timedelta(hours=1)
    if activity == 'pending_upload':
        active = UploadSession.objects.create(**common, uploader=author, expected_size_bytes=32, filename='process.mp4', expires_at=expiry)
        terminal = 'cancelled'
    elif activity == 'recording':
        active = VisualSession.objects.create(**common, mode='live', created_by=author,
                                              base_working_revision=working['revision'], language='ru', expires_at=expiry)
        terminal = 'completed'
    else:
        active = ProcessingJob.objects.create(**common, kind='analyze_live', requested_by=author,
                                              state='queued' if activity == 'queued_job' else 'running')
        terminal = 'done'
    client = team.clients['admin']
    denied = client.delete(url, HTTP_IF_MATCH=working['etag'])
    assert denied.status_code == 409 and denied.json()['error']['code'] == 'DRAFT_PROCESSING_ACTIVE'
    active.state = terminal
    active.save(update_fields=['state'])
    assert client.delete(url, HTTP_IF_MATCH=working['etag']).status_code == 204


def test_review_cannot_be_deleted(team, recipe):
    url, working = recipe
    instruction = Instruction.objects.get(pk=working['instruction_id'])
    instruction.working_copy.workflow = 'in_review'
    instruction.working_copy.save()
    client = team.clients['admin']
    assert not client.get(url + '/working-copy').json()['can_delete_draft']
    assert client.delete(url, HTTP_IF_MATCH=working['etag']).status_code == 409
    assert recipe_count(team.org) == 1


def test_unpublished_legacy_document_becomes_removable_only_when_saved_as_recipe(team, draft):
    author = team.clients['author']
    assert not draft.working['can_delete_draft']
    assert author.delete(draft.url, HTTP_IF_MATCH=draft.working['etag']).status_code == 409
    converted = save(author, draft.url, draft.working, document(draft.working, recipe={'ingredients': ['Мука'], 'servings': 1}))
    assert converted.status_code == 200 and converted.json()['can_delete_draft']
    assert author.delete(draft.url, HTTP_IF_MATCH=converted.json()['etag']).status_code == 204


def test_withdrawn_publication_cannot_be_deleted(team, publication):
    url = publication.url
    admin = team.clients['admin']
    latest = admin.get(url + '/working-copy').json()
    assert admin.delete(url, HTTP_IF_MATCH=latest['etag']).status_code == 409
    reopened = post(admin, url + '/create-draft', {'source_version_id': publication.version['id']})
    assert reopened.status_code == 201
    converted = save(admin, url, reopened.json(), document(reopened.json(), recipe={'ingredients': ['Мука'], 'servings': 1}))
    assert converted.status_code == 200
    withdrawn = post(admin, url + '/withdraw-publication', {'reason': 'Рецепт требует проверки.', 'current_version_id': publication.version['id']})
    assert withdrawn.status_code == 200
    latest = admin.get(url + '/working-copy').json()
    assert not latest['can_delete_draft']
    assert admin.delete(url, HTTP_IF_MATCH=latest['etag']).status_code == 409
    assert admin.get(url + '/versions').json()['items'][0]['id'] == publication.version['id']
