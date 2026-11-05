import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from core.models import Membership
from .conftest import base, post

pytestmark = pytest.mark.django_db


def test_anonymous_api_returns_json_401_with_request_id():
    response = APIClient().get('/api/v1/auth/me')
    assert response.status_code == 401
    assert response.json()['error']['code'] == 'AUTH_REQUIRED'
    assert response.json()['error']['request_id'] == response['X-Request-ID']
    assert response['Cache-Control'] == 'private, no-store'


def test_real_login_and_mutation_require_csrf(team):
    client = APIClient(enforce_csrf_checks=True)
    payload = {'email': 'author@tests.local', 'password': 'Test-password-2026!'}
    denied = post(client, '/api/v1/auth/login', payload)
    assert denied.status_code == 403
    assert denied.json()['error']['code'] == 'CSRF_FAILED'
    csrf = client.get('/api/v1/auth/csrf').json()['csrf_token']
    login = client.post('/api/v1/auth/login', payload, format='json', HTTP_X_CSRFTOKEN=csrf)
    assert login.status_code == 200
    assert login.json()['organizations'][0]['role'] == 'author'
    assert client.cookies['sessionid']['httponly']
    assert client.get('/api/v1/auth/me').status_code == 200
    assert post(client, '/api/v1/auth/logout').status_code == 403
    token = client.get('/api/v1/auth/csrf').json()['csrf_token']
    assert client.post('/api/v1/auth/logout', {}, format='json', HTTP_X_CSRFTOKEN=token).status_code == 204
    assert client.get('/api/v1/auth/me').status_code == 401


def test_login_rate_limit_is_generic_and_temporary(team):
    client = APIClient()
    for _ in range(5):
        response = post(client, '/api/v1/auth/login', {'email': 'author@tests.local', 'password': 'incorrect'})
        assert response.status_code == 401
        assert response.json()['error']['code'] == 'INVALID_CREDENTIALS'
    assert post(client, '/api/v1/auth/login', {'email': 'author@tests.local', 'password': 'Test-password-2026!'}).status_code == 429
    cache.clear()
    assert post(client, '/api/v1/auth/login', {'email': 'author@tests.local', 'password': 'Test-password-2026!'}).status_code == 200


def test_reader_cannot_discover_draft_or_modify(team, draft):
    client = team.clients['reader']
    assert client.get(base(team)).json()['items'] == []
    assert client.get(draft.url).status_code == 404
    assert client.get(draft.url + '/working-copy').status_code == 403
    assert post(client, base(team), {}, key='reader-create').status_code == 403
    assert client.get(draft.working['source']['playback_url']).status_code == 404


def test_foreign_tenant_ids_do_not_reveal_objects(team, draft):
    assert team.outsider.get(draft.url).status_code == 404
    own_prefix = f'/api/v1/orgs/{team.other.id}/instructions/{draft.working["instruction_id"]}'
    assert team.outsider.get(own_prefix).status_code == 404
    assert team.outsider.get(draft.working['source']['playback_url']).status_code == 404
    own_asset = draft.working['source']['playback_url'].replace(str(team.org.id), str(team.other.id))
    assert team.outsider.get(own_asset).status_code == 404


def test_author_cannot_review_or_publish(team, draft):
    for action in ('review/check-step', 'publish', 'return-to-draft', 'withdraw-publication'):
        response = post(team.clients['author'], draft.url + '/' + action, {}, etag=draft.working['etag'], key='no-rights')
        assert response.status_code == 403


def test_reader_gets_only_current_snapshot_no_versions(team, publication):
    client = team.clients['reader']
    response = client.get(publication.url)
    assert response.status_code == 200
    assert response.json()['working_copy'] is None
    assert response.json()['publication']['id'] == publication.version['id']
    assert client.get(publication.url + '/versions').status_code == 403
    assert client.get(publication.url + '/versions/' + publication.version['id']).status_code == 403


def test_revoked_membership_and_suspended_org_apply_next_request(team, publication):
    reader = team.clients['reader']
    media = publication.version['snapshot']['source']['playback_url']
    assert reader.get(publication.url).status_code == 200
    Membership.objects.filter(user=team.users['reader']).update(is_active=False)
    assert reader.get(publication.url).status_code == 404
    assert reader.get(media).status_code == 404
    team.org.status = 'suspended'
    team.org.save()
    assert team.clients['author'].get(publication.url).status_code == 403
