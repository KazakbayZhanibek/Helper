import uuid
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from core.models import Membership, Organization, User


@pytest.fixture(autouse=True)
def isolated_settings(settings, tmp_path):
    settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
    settings.DEMO_MODE = True
    settings.PRIVATE_MEDIA_ROOT = tmp_path
    (tmp_path / 'demo-process.mp4').write_bytes(bytes(range(256)) * 10)
    cache.clear()


@pytest.fixture
def team(db):
    org = Organization.objects.create(name='Тестовая мастерская')
    other = Organization.objects.create(name='Другая организация')
    users, clients = {}, {}
    for role in ('admin', 'author', 'reviewer', 'reader'):
        user = User.objects.create_user(email=f'{role}@tests.local', password='Test-password-2026!', display_name=role)
        Membership.objects.create(organization=org, user=user, role=role)
        client = APIClient()
        client.force_login(user)
        users[role], clients[role] = user, client
    other_user = User.objects.create_user(email='foreign@tests.local', password='Test-password-2026!', display_name='Чужой автор')
    Membership.objects.create(organization=other, user=other_user, role='author')
    outsider = APIClient()
    outsider.force_login(other_user)
    return SimpleNamespace(org=org, other=other, users=users, clients=clients, outsider=outsider)


def base(team):
    return f'/api/v1/orgs/{team.org.id}/instructions'


def post(client, url, payload=None, etag=None, key=None):
    headers = {}
    if etag:
        headers['HTTP_IF_MATCH'] = etag
    if key:
        headers['HTTP_IDEMPOTENCY_KEY'] = key
    return client.post(url, payload if payload is not None else {}, format='json', **headers)


def document(working, **changes):
    value = {key: working[key] for key in ('title', 'purpose', 'language')}
    value['steps'] = [{key: step[key] for key in ('id', 'title', 'description', 'start_ms', 'end_ms', 'evidence_segment_ids')} for step in working['steps']]
    value.update(changes)
    return value


def save(client, url, working, payload=None):
    return client.put(url + '/working-copy', payload or document(working), format='json', HTTP_IF_MATCH=working['etag'])


@pytest.fixture
def draft(team):
    response = post(team.clients['author'], base(team), {'title': 'Собрать учебный набор', 'purpose': 'Проверить комплект перед упаковкой', 'language': 'ru', 'demo_source': True}, key=str(uuid.uuid4()))
    assert response.status_code == 201, response.data
    working = response.json()
    url = base(team) + '/' + working['instruction_id']
    payload = document(working, steps=[
        {'id': None, 'title': 'Проверить состав', 'description': 'Сверьте предметы со списком.', 'start_ms': 0, 'end_ms': 15000, 'evidence_segment_ids': []},
        {'id': None, 'title': 'Уложить в коробку', 'description': 'Поместите три предмета в коробку.', 'start_ms': 15000, 'end_ms': 60000, 'evidence_segment_ids': []},
    ])
    response = save(team.clients['author'], url, working, payload)
    assert response.status_code == 200, response.data
    return SimpleNamespace(url=url, working=response.json())


CONFIRMATIONS = {'order': True, 'terminology': True, 'completeness': True, 'relevance': True, 'privacy': True}


def checked_review(team, draft):
    response = post(team.clients['author'], draft.url + '/submit-review', etag=draft.working['etag'])
    assert response.status_code == 200, response.data
    working = response.json()
    for step in working['steps']:
        response = post(team.clients['reviewer'], draft.url + '/review/check-step', {'step_id': step['id'], 'checked': True}, etag=working['etag'])
        assert response.status_code == 200, response.data
        working = response.json()
    return working


@pytest.fixture
def publication(team, draft):
    working = checked_review(team, draft)
    response = post(team.clients['reviewer'], draft.url + '/publish', {'confirmations': CONFIRMATIONS}, etag=working['etag'], key=str(uuid.uuid4()))
    assert response.status_code == 201, response.data
    return SimpleNamespace(url=draft.url, version=response.json(), working=working)
