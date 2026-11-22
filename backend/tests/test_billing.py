"""Exercise the HTTP checkout, signed provider callbacks and real resource limits offline."""
import base64
import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from django.core.cache import cache
from rest_framework.test import APIClient

from billing import provider, services
from billing.models import Checkout, LegacyLibrary, WebhookReceipt
from core.http import DomainError
from core.models import Membership
from customer_accounts.models import EmailIdentity
from tests.conftest import base, post

WEBHOOK = '/api/v1/billing/webhooks/dodo'
SECRET = b'example webhook signing bytes, for offline tests'
CHECKOUT_URL = 'https://test.checkout.dodopayments.com/example'
pytestmark = pytest.mark.django_db


@pytest.fixture
def merchant(settings):
    settings.DODO_MODE = 'test'
    settings.DODO_WEBHOOK_SECRET = 'whsec_' + base64.b64encode(SECRET).decode()
    settings.DODO_API_KEY = 'offline-key'
    settings.DODO_PRODUCT_ID = 'pdt_team'
    settings.DODO_BUSINESS_ID = 'bus_kitchen'
    settings.PUBLIC_APP_URL = 'https://recipes.example'
    cache.set('billing:offer:test:bus_kitchen:pdt_team', '9 USD / месяц', 60)


def endpoint(team):
    return f'/api/v1/orgs/{team.org.pk}/billing'


def open_checkout(team):
    with patch('billing.provider.request_json', return_value={'session_id': 'cks_test', 'checkout_url': CHECKOUT_URL}) as transport:
        response = team.clients['admin'].post(endpoint(team) + '/checkout', {}, format='json')
    assert response.status_code == 200, response.data
    assert response.data == {'url': CHECKOUT_URL}
    row = Checkout.objects.get(organization=team.org)
    method, path, body = transport.call_args.args
    assert (method, path) == ('POST', '/checkouts')
    assert body['feature_flags']['always_create_new_customer']
    assert body['product_cart'] == [{'product_id': 'pdt_team', 'quantity': 1}]
    assert body['metadata']['recipe_checkout'] == str(row.pk)
    assert body['return_url'] == 'https://recipes.example/o/' + str(team.org.pk) + '/billing?checkout=return'
    return row


def event_for(row, *, status='active', occurred=None, period_end=None, **changes):
    now = timezone.now()
    data = {'subscription_id': 'sub_test', 'product_id': 'pdt_team',
            'status': status, 'customer': {'customer_id': 'cus_buyer'}, 'metadata': {'recipe_checkout': str(row.pk)},
            'next_billing_date': (period_end or now + timedelta(days=31)).isoformat(),
            'cancel_at_next_billing_date': False, **changes}
    return {'business_id': 'bus_kitchen', 'type': 'subscription.updated', 'data': data,
            'timestamp': (occurred or now).isoformat()}


def deliver(event, *, event_id=None, timestamp=None, secret=SECRET):
    raw = json.dumps(event).encode()
    event_id = event_id or 'msg_' + uuid.uuid4().hex
    timestamp = str(timestamp or int(timezone.now().timestamp()))
    signature = base64.b64encode(hmac.digest(secret, f'{event_id}.{timestamp}.'.encode() + raw, hashlib.sha256)).decode()
    return APIClient().post(WEBHOOK, raw, content_type='application/json',
                            HTTP_WEBHOOK_ID=event_id, HTTP_WEBHOOK_TIMESTAMP=timestamp,
                            HTTP_WEBHOOK_SIGNATURE='v1,' + signature)


def new_recipe(team, *, legacy=False):
    data = {'title': 'Гречневая каша', 'purpose': 'Приготовить кашу из гречки', 'language': 'ru'}
    if not legacy:
        data['recipe'] = {'ingredients': ['Гречка'], 'servings': 2}
    return post(team.clients['author'], base(team), data, key=str(uuid.uuid4()))


def test_default_free_plan_and_permissions(team):
    assert [p['code'] for p in APIClient().get('/api/v1/billing/plans').data['plans']] == ['free', 'team']
    assert APIClient().get(endpoint(team)).status_code == 401
    assert team.outsider.get(endpoint(team)).status_code == 404
    state = team.clients['reader'].get(endpoint(team)).json()
    assert state['plan'] == 'free' and not state['can_manage']
    assert state['usage']['members'] == 4  # Earlier memberships are retained, even over the new cap.
    assert not state['checkout_available'] and not state['portal_available']
    assert team.clients['author'].post(endpoint(team) + '/checkout', {}, format='json').status_code == 403
    assert team.clients['admin'].post(endpoint(team) + '/checkout', {}, format='json').status_code == 503
    with pytest.raises(DomainError, match='участников'):
        services.enforce_member_limit(team.org)


def test_checkout_must_be_verified_and_cannot_supply_plan_or_amount(team, merchant):
    EmailIdentity.objects.create(user=team.users['admin'], email=team.users['admin'].email)
    with patch('billing.provider.request_json') as transport:
        result = team.clients['admin'].post(endpoint(team) + '/checkout', {}, format='json')
        assert result.status_code == 403 and result.json()['error']['code'] == 'EMAIL_VERIFICATION_REQUIRED'
        assert team.clients['admin'].post(endpoint(team) + '/checkout', {'price': 0}, format='json').status_code == 400
        transport.assert_not_called()
    assert not Checkout.objects.exists()


def test_checkout_is_reused_and_does_not_activate_subscription(team, merchant):
    open_checkout(team)
    with patch('billing.provider.request_json') as transport:
        assert team.clients['admin'].post(endpoint(team) + '/checkout', {}, format='json').data == {'url': CHECKOUT_URL}
        assert not transport.called
    assert services.entitlement(team.org)['plan'] == 'free'
    assert team.clients['admin'].post(endpoint(team) + '/portal', {}, format='json').status_code == 403
    Membership.objects.filter(organization=team.org, user=team.users['author']).update(role='admin')
    assert team.clients['author'].post(endpoint(team) + '/checkout', {}, format='json').status_code == 409


@pytest.mark.parametrize('payment_status', ['failed', 'processing', 'succeeded'])
def test_card_retry_replaces_only_definitively_failed_payment(team, merchant, payment_status):
    row = open_checkout(team)
    Checkout.objects.filter(pk=row.pk).update(created_at=timezone.now() - timedelta(minutes=1))
    session = {'id': row.session_id, 'payment_id': 'pay_test', 'payment_status': payment_status}
    with patch('billing.provider.request_json', side_effect=[session, {'session_id': 'cks_second', 'checkout_url': CHECKOUT_URL}]) as call:
        response = team.clients['admin'].post(endpoint(team) + '/checkout', {}, format='json')
    if payment_status == 'failed':
        assert response.status_code == 200 and Checkout.objects.count() == 2
        assert call.call_count == 2
    else:
        assert response.status_code == 409 and Checkout.objects.count() == 1
        assert call.call_count == 1


def test_signed_webhooks_activate_extend_and_cancel_preserving_work(team, merchant):
    row = open_checkout(team)
    for i in range(5):
        assert new_recipe(team, legacy=i == 0).status_code == 201
    assert new_recipe(team).status_code == 409
    assert new_recipe(team, legacy=True).status_code == 409
    stamp = timezone.now()
    activated = event_for(row, occurred=stamp)
    assert deliver(activated, event_id='msg_original').status_code == 204
    assert deliver(activated, event_id='msg_original').status_code == 204
    assert WebhookReceipt.objects.count() == 1
    assert new_recipe(team).status_code == 201
    scheduled = event_for(row, occurred=stamp + timedelta(seconds=2), cancel_at_next_billing_date=True)
    assert deliver(scheduled).status_code == 204
    state = team.clients['admin'].get(endpoint(team)).data
    assert state['plan'] == 'team' and state['cancel_at_period_end']
    assert state['portal_available'] and not state['checkout_available']
    assert team.clients['admin'].post(endpoint(team) + '/checkout', {}, format='json').status_code == 409
    assert deliver(event_for(row, status='cancelled', occurred=stamp + timedelta(seconds=4))).status_code == 204
    assert deliver(scheduled).status_code == 204
    state = team.clients['admin'].get(endpoint(team)).data
    assert state['plan'] == 'free' and state['usage']['recipes'] == 6 and state['checkout_available']
    assert new_recipe(team).status_code == 409 and services.recipe_count(team.org) == 6


def test_portal_belongs_only_to_verified_paying_admin_and_customer(team, merchant):
    row = open_checkout(team)
    assert deliver(event_for(row)).status_code == 204
    Membership.objects.filter(organization=team.org, user=team.users['author']).update(role='admin')
    with patch('billing.provider.request_json', return_value={'link': 'https://customer.dodopayments.com/example'}) as outbound:
        assert team.clients['author'].post(endpoint(team) + '/portal', {}, format='json').status_code == 403
        assert team.outsider.post(endpoint(team) + '/portal', {}, format='json').status_code == 404
        assert team.clients['admin'].post(endpoint(team) + '/portal', {}, format='json').status_code == 200
    assert outbound.call_count == 1 and outbound.call_args.args[1].startswith('/customers/cus_buyer/customer-portal/session?')


def test_signature_replay_business_and_environment_boundaries(team, merchant, settings):
    row = open_checkout(team)
    event = event_for(row)
    assert deliver(event, secret=b'incorrect key').status_code == 401
    assert deliver(event, timestamp=int(timezone.now().timestamp()) - 800).status_code == 401
    assert deliver({**event, 'business_id': 'elsewhere'}).status_code == 403
    assert deliver(event_for(row, product_id='some_other_product')).status_code == 204
    assert deliver(event_for(row, metadata={'recipe_checkout': str(uuid.uuid4())})).status_code == 204
    assert not WebhookReceipt.objects.exists() and services.entitlement(team.org)['plan'] == 'free'
    assert deliver(event).status_code == 204
    settings.DODO_MODE = 'live'
    assert services.entitlement(team.org)['plan'] == 'free'
    assert deliver(event).status_code == 204
    assert not Checkout.objects.filter(mode='live').exists()


def test_expiry_and_other_product_revoke_quota_even_when_callback_stale(team, merchant):
    row = open_checkout(team)
    now = timezone.now()
    assert deliver(event_for(row, occurred=now, period_end=now-timedelta(seconds=5))).status_code == 204
    assert services.entitlement(team.org)['plan'] == 'free'
    assert deliver(event_for(row, occurred=now+timedelta(seconds=5), product_id='different')).status_code == 204
    assert services.entitlement(team.org)['status'] == 'other_product'
    assert deliver(event_for(row, occurred=now+timedelta(seconds=1))).status_code == 204
    assert services.entitlement(team.org)['plan'] == 'free'


def test_exempt_legacy_archive_only_counts_when_converted(team):
    old = new_recipe(team, legacy=True).json()
    LegacyLibrary.objects.create(organization=team.org, upgraded_at=timezone.now())
    assert services.recipe_count(team.org) == 0
    for _ in range(5):
        assert new_recipe(team).status_code == 201
    url = base(team) + '/' + old['instruction_id'] + '/working-copy'
    edit = {k: old[k] for k in ('title', 'purpose', 'steps', 'language')}
    result = team.clients['author'].put(url, {**edit, 'recipe': {'ingredients': ['Укроп'], 'servings': None}},
                                        format='json', HTTP_IF_MATCH=old['etag'])
    assert result.status_code == 409
    assert team.clients['author'].put(url, edit, format='json', HTTP_IF_MATCH=old['etag']).status_code == 200


def test_public_catalog_and_checkout_require_real_recurring_product(merchant, team):
    cache.clear()
    product = {'business_id': 'bus_kitchen', 'product_id': 'pdt_team', 'is_recurring': True,
               'price': {'price': 149900, 'currency': 'KZT', 'type': 'recurring_price',
                         'payment_frequency_count': 1, 'payment_frequency_interval': 'month'}}
    with patch('billing.provider.request_json', return_value=product) as transport:
        for _ in range(2):
            assert APIClient().get('/api/v1/billing/plans').json()['plans'][1]['price_label'] == '1499 KZT / месяц'
    transport.assert_called_once_with('GET', '/products/pdt_team', timeout=3)
    cache.clear()
    with patch('billing.provider.request_json', return_value={**product, 'is_recurring': False}) as transport:
        assert not team.clients['admin'].get(endpoint(team)).json()['checkout_available']
        assert team.clients['admin'].post(endpoint(team) + '/checkout', {}, format='json').status_code == 503
    transport.assert_called_once()


def test_refresh_recovers_missed_webhook_from_server_owned_payment_chain(team, merchant):
    row = open_checkout(team)
    session = {'id': row.session_id, 'payment_id': 'pay_test', 'payment_status': 'succeeded'}
    payment = {'business_id': 'bus_kitchen', 'checkout_session_id': row.session_id, 'payment_id': 'pay_test',
               'status': 'succeeded', 'subscription_ids': ['sub_test']}
    with patch('billing.provider.request_json', side_effect=[session, payment, event_for(row)['data']]) as outbound:
        state = team.clients['admin'].post(endpoint(team) + '/refresh', {}, format='json')
        assert state.status_code == 200 and state.data['plan'] == 'team'
        assert team.clients['admin'].post(endpoint(team) + '/refresh', {}, format='json').status_code == 200
    assert [x.args[1] for x in outbound.call_args_list] == ['/checkouts/cks_test', '/payments/pay_test', '/subscriptions/sub_test']
    assert not WebhookReceipt.objects.exists()


def test_refresh_rejects_foreign_payment_and_ignores_browser_claims(team, merchant):
    row = open_checkout(team)
    session = {'id': row.session_id, 'payment_id': 'pay_test', 'payment_status': 'succeeded'}
    payment = {'business_id': 'bus_kitchen', 'checkout_session_id': 'a-different-checkout', 'payment_id': 'pay_test',
               'status': 'succeeded', 'subscription_ids': ['sub_test']}
    with patch('billing.provider.request_json', side_effect=[session, payment]) as outbound:
        admin = team.clients['admin']
        assert admin.post(endpoint(team) + '/refresh', {'subscription_id': 'sub_test'}, format='json').status_code == 400
        assert admin.post(endpoint(team) + '/refresh', {}, format='json').data['plan'] == 'free'
    assert outbound.call_count == 2


def test_webhook_key_accepts_standard_unpadded_base64(settings):
    settings.DODO_WEBHOOK_SECRET = 'whsec_' + base64.b64encode(b'0123456789abcdef').decode().rstrip('=')
    assert provider.signing_key() == b'0123456789abcdef'


@pytest.mark.parametrize('value', ['http://checkout.dodopayments.com/a', 'https://evil.test/a',
                                   'https://checkout.dodopayments.com@evil.test/', 'https://checkout.dodopayments.com:8080/a'])
def test_return_links_only_use_provider_https(value):
    with pytest.raises(DomainError, match='защищённую'):
        provider.hosted_url(value)
