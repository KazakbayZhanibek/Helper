import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Organization, User
from customer_accounts.models import EmailIdentity
from .conftest import post


pytestmark = pytest.mark.django_db
CUSTOMERS = '/api/v1/operator/customers'


def test_only_operator_can_search_and_page_through_customers(team):
    operator = User.objects.create_superuser('operator@example.test', 'test-only', display_name='Оператор')
    client = APIClient()
    assert client.get(CUSTOMERS).status_code == 401
    assert team.clients['admin'].get(CUSTOMERS).status_code == 403
    client.force_login(operator)
    session = client.get('/api/v1/auth/me').json()
    assert session['user']['is_operator'] and session['organizations'] == []
    assert client.get(f'/api/v1/orgs/{team.org.pk}/instructions').status_code == 404
    grant = client.get(CUSTOMERS, {'q': team.users['admin'].email}).json()['items']
    assert len(grant) == 1
    assert (grant[0]['id'], grant[0]['members'], grant[0]['plan']) == (str(team.org.pk), 4, 'free')
    assert client.get(CUSTOMERS, {'q': 'missing!'}).json()['items'] == []
    Organization.objects.bulk_create([Organization(name=f'Кухня {i}') for i in range(27)])
    first = client.get(CUSTOMERS, {'q': 'Кухня'}).json()
    second = client.get(CUSTOMERS, {'q': 'Кухня', 'offset': first['next_offset']}).json()
    assert len(first['items']) == 25 and len(second['items']) == 2
    assert not set(r['id'] for r in first['items']).intersection(r['id'] for r in second['items'])
    assert second['next_offset'] is None
    assert client.get(CUSTOMERS, {'offset': '-1'}).status_code == 400


def test_kitchen_ai_requires_consent_verified_admin_and_free_route(team, settings):
    settings.EXTERNAL_AI_ENABLED = True
    settings.STEP_DRAFT_BACKEND = 'openrouter'
    settings.OPENROUTER_API_KEY = 'offline-test'
    settings.STEP_DRAFT_ENDPOINT = 'https://openrouter.ai/api/v1'
    settings.STEP_DRAFT_MODEL = 'openrouter/free'
    owner = team.users['admin']
    identity = EmailIdentity.objects.create(user=owner, email=owner.email)
    client = team.clients['admin']
    path = f'/api/v1/orgs/{team.org.pk}/ai-settings'
    assert client.get(path).json() == {'enabled': False, 'can_enable': True}
    assert team.outsider.get(path).status_code == 404
    assert post(team.clients['author'], path, {'enabled': True}).status_code == 403
    assert post(client, path, {'enabled': True}).json()['error']['code'] == 'EMAIL_VERIFICATION_REQUIRED'
    identity.verified_at = timezone.now()
    identity.save()
    assert post(client, path, {'enabled': True}).json()['enabled']
    settings.STEP_DRAFT_MODEL = 'openrouter/auto'
    assert post(client, path, {'enabled': False}).json() == {'enabled': False, 'can_enable': False}
    assert post(client, path, {'enabled': True}).json()['error']['code'] == 'AI_UNAVAILABLE'
    team.org.refresh_from_db()
    assert not team.org.external_ai_enabled
