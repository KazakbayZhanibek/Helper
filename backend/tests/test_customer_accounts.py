from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Membership, Organization, User
from customer_accounts.delivery import deliver_one
from customer_accounts.models import AccountMail, AccountRateLimit, EmailIdentity, Invitation

pytestmark = pytest.mark.django_db
ROOT = '/api/v1'
PASSWORD = 'Basil-lemon-and-olive-728!'
NEW_PASSWORD = 'A-second-pantry-lock-475!'


@pytest.fixture(autouse=True)
def email_settings(settings):
    settings.APP_PUBLIC_URL = 'https://kitchen.example.org'
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'


def post(client, path, data):
    return client.post(ROOT + path, data, format='json')


def signup(client, email='cook@example.org', **changes):
    data = {'email': email, 'password': PASSWORD, 'display_name': 'Повар', 'kitchen_name': 'Тестовая кухня'}
    return post(client, '/auth/register', {**data, **changes})


def email_link():
    assert deliver_one()
    return next(line for line in mail.outbox[-1].body.splitlines() if line.startswith('https://'))


def link_params(url):
    assert urlsplit(url).netloc == 'kitchen.example.org'
    return {key: value[0] for key, value in parse_qs(urlsplit(url).query).items()}


def confirm(client):
    assert post(client, '/auth/email/verify', link_params(email_link())).status_code == 200


def test_signup_creates_admin_free_kitchen_and_verifies_email():
    client = APIClient()
    response = signup(client, 'Cook@EXAMPLE.ORG')
    assert response.status_code == 201
    session = response.json()
    assert session['user']['email'] == 'cook@example.org' and not session['user']['email_verified']
    org = session['organizations'][0]
    assert len(session['organizations']) == 1 and org['role'] == 'admin'
    assert client.get(ROOT + '/auth/me').json() == session
    assert AccountMail.objects.filter(kind='verify').count() == 1
    confirm(client)
    assert client.get(ROOT + '/auth/me').json()['user']['email_verified']
    assert not deliver_one()
    assert post(client, '/auth/email/resend', {}).status_code == 202
    assert AccountMail.objects.count() == 0


def test_invalid_or_duplicate_signup_cannot_create_kitchen_or_switch_identity():
    client = APIClient()
    assert signup(client, password='x').status_code == 400
    assert signup(client, email='not-an-address').status_code == 400
    assert signup(client, kitchen_name='Название\nпример').status_code == 400
    assert not Organization.objects.exists()
    assert signup(client).status_code == 201
    assert signup(client, 'other@example.org').status_code == 409
    assert signup(APIClient(), 'COOK@EXAMPLE.ORG').status_code == 409
    assert User.objects.count() == Organization.objects.count() == EmailIdentity.objects.count() == 1


def test_public_account_posts_require_csrf():
    browser = APIClient(enforce_csrf_checks=True)
    assert signup(browser).status_code == 403
    for path in ['/auth/password/forgot', '/auth/email/verify', '/auth/password/reset', '/auth/invitations/unknown']:
        assert post(browser, path, {}).status_code == 403
    nonce = browser.get(ROOT + '/auth/csrf').json()['csrf_token']
    browser.credentials(HTTP_X_CSRFTOKEN=nonce)
    assert signup(browser).status_code == 201


def test_recovery_response_is_uniform_and_password_token_cannot_be_reused():
    client, outsider = APIClient(), APIClient()
    signup(client)
    assert deliver_one()
    assert post(outsider, '/auth/password/forgot', {'email': 'unknown@example.org'}).status_code == 202
    assert not AccountMail.objects.exists()
    known = post(outsider, '/auth/password/forgot', {'email': 'Cook@example.org'})
    assert known.status_code == 202 and known.json() == {'ok': True}
    params = link_params(email_link())
    assert post(outsider, '/auth/password/reset', {**params, 'password': 'short'}).status_code == 400
    assert post(outsider, '/auth/password/reset', {**params, 'password': NEW_PASSWORD}).status_code == 200
    assert post(outsider, '/auth/password/reset', {**params, 'password': NEW_PASSWORD}).status_code == 410
    assert client.get(ROOT + '/auth/me').status_code == 401
    assert post(outsider, '/auth/login', {'email': 'cook@example.org', 'password': PASSWORD}).status_code == 401
    again = post(outsider, '/auth/login', {'email': 'cook@example.org', 'password': NEW_PASSWORD})
    assert again.status_code == 200 and again.json()['user']['email_verified']


def test_verification_links_are_bound_to_mailbox_and_have_time_limits(monkeypatch):
    client = APIClient()
    signup(client)
    params = link_params(email_link())
    user = User.objects.get()
    user.email = 'changed@example.org'
    user.save(update_fields=['email'])
    assert post(client, '/auth/email/verify', params).status_code == 410
    user.email = 'cook@example.org'
    user.save(update_fields=['email'])
    from django.core import signing
    now = signing.time.time()
    monkeypatch.setattr(signing.time, 'time', lambda: now + 86410)
    assert post(client, '/auth/email/verify', params).status_code == 410


def test_forgot_mail_retry_is_durable_and_stale_request_is_not_sent(monkeypatch):
    from customer_accounts import delivery

    client = APIClient()
    signup(client)
    assert deliver_one()
    post(client, '/auth/password/forgot', {'email': 'cook@example.org'})
    with monkeypatch.context() as patch:
        def unavailable(*args, **kwargs):
            raise OSError()
        patch.setattr(delivery, 'send_mail', unavailable)
        assert deliver_one() and not deliver_one()
    queued = AccountMail.objects.get()
    assert queued.attempts == 1 and queued.available_at > timezone.now()
    user = User.objects.get()
    user.set_password(NEW_PASSWORD)
    user.save(update_fields=['password'])
    AccountMail.objects.update(available_at=timezone.now())
    assert deliver_one() and len(mail.outbox) == 1 and not AccountMail.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_dedicated_worker_delivers_to_configured_development_mailbox(settings, tmp_path):
    from email import policy
    from email.parser import BytesParser
    settings.EMAIL_BACKEND = 'django.core.mail.backends.filebased.EmailBackend'
    settings.EMAIL_FILE_PATH = tmp_path / 'mail'
    response = signup(APIClient())
    assert response.status_code == 201 and not settings.EMAIL_FILE_PATH.exists()
    call_command('deliver_account_mail', once=True)
    path, = settings.EMAIL_FILE_PATH.iterdir()
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    assert message['To'] == 'cook@example.org'
    assert 'https://kitchen.example.org/verify-email?' in message.get_body().get_content()
    assert not AccountMail.objects.exists()


def test_reset_rate_limit_survives_another_client():
    first = APIClient()
    signup(first)
    assert deliver_one()
    for _ in range(5):
        assert post(APIClient(), '/auth/password/forgot', {'email': 'cook@example.org'}).status_code == 202
    assert AccountMail.objects.filter(kind='reset').count() == 3
    assert AccountRateLimit.objects.exists()


def test_invite_new_user_accept_revoke_remove_and_reinvite():
    admin, guest, outsider = APIClient(), APIClient(), APIClient()
    org = signup(admin).json()['organizations'][0]
    team = '/orgs/' + org['id'] + '/team'
    invites = '/orgs/' + org['id'] + '/invitations'
    data = {'email': 'reader@example.org', 'role': 'reader'}
    assert post(admin, invites, data).status_code == 403
    confirm(admin)
    assert post(admin, invites, {**data, 'role': 'admin'}).status_code == 400
    invite = post(admin, invites, data).json()
    href = '/auth/invitations/' + invite['invite_url'].rsplit('/', 1)[1]
    assert email_link() != invite['invite_url']
    hint = guest.get(ROOT + href).json()
    assert hint['organization_id'] == org['id'] and hint['email_hint'] != data['email']
    assert hint['role'] == 'reader' and not hint['has_account']
    accepted = post(guest, href, {'display_name': 'Читатель', 'password': PASSWORD})
    assert accepted.status_code == 200
    assert accepted.json()['user']['email'] == data['email']
    assert not accepted.json()['user']['email_verified']
    assert accepted.json()['organizations'] == [{**org, 'role': 'reader'}]
    assert post(guest, href, {}).status_code == 200
    assert post(outsider, href, {}).status_code == 410
    assert guest.get(ROOT + team).status_code == 403
    members = admin.get(ROOT + team).json()
    assert not members['invitations']
    owner, reader = members['members']
    assert admin.delete(ROOT + team + '/' + owner['id']).status_code == 409
    assert admin.delete(ROOT + team + '/' + reader['id']).status_code == 204
    assert post(guest, href, {}).json()['organizations'] == []
    assert guest.get(ROOT + '/orgs/' + org['id'] + '/instructions').status_code == 404
    invite = post(admin, invites, data).json()
    assert admin.delete(ROOT + invites + '/' + invite['id']).status_code == 204
    assert guest.get(ROOT + '/auth/invitations/' + invite['invite_url'].rsplit('/', 1)[1]).status_code == 410


def test_existing_invite_requires_matching_session_and_preserves_existing_roles(team):
    admin, registered = APIClient(), team.outsider
    org = signup(admin).json()['organizations'][0]
    confirm(admin)
    invite = post(admin, '/orgs/' + org['id'] + '/invitations', {'role': 'reviewer', 'email': 'foreign@tests.local'}).json()
    href = '/auth/invitations/' + invite['invite_url'].rsplit('/', 1)[1]
    forged = {'display_name': 'Fake', 'password': NEW_PASSWORD}
    assert post(APIClient(), href, forged).status_code == 409
    assert post(team.clients['admin'], href, forged).status_code == 403
    assert registered.get(ROOT + href).json()['matches_session']
    accepted = post(registered, href, {})
    assert accepted.status_code == 200
    roles = {org['id']: org['role'] for org in accepted.json()['organizations']}
    assert roles == {str(team.other.pk): 'author', org['id']: 'reviewer'}
    assert not User.objects.get(email='foreign@tests.local').check_password(NEW_PASSWORD)


def test_only_link_delivered_to_mailbox_can_verify_new_invited_account():
    admin = APIClient()
    org = signup(admin).json()['organizations'][0]
    confirm(admin)
    issued = post(admin, '/orgs/' + org['id'] + '/invitations',
                  {'role': 'author', 'email': 'author@example.org'}).json()
    copied = issued['invite_url'].rsplit('/', 1)[1]
    received = email_link().rsplit('/', 1)[1]
    assert received != copied
    guest = APIClient()
    assert guest.get(ROOT + '/auth/invitations/email.' + copied).status_code == 410
    accepted = post(guest, '/auth/invitations/' + received, {'display_name': 'Автор', 'password': PASSWORD})
    assert accepted.status_code == 200 and accepted.json()['user']['email_verified']
    assert not AccountMail.objects.exists()


def test_pending_seats_count_and_expiry_releases_them():
    admin = APIClient()
    org = signup(admin).json()['organizations'][0]
    confirm(admin)
    base = '/orgs/' + org['id'] + '/invitations'
    for user in ['one', 'two']:
        assert post(admin, base, {'email': user + '@example.org', 'role': 'author'}).status_code == 201
    assert post(admin, base, {'email': 'three@example.org', 'role': 'reader'}).status_code == 409
    first = Invitation.objects.get(email='one@example.org')
    original = post(admin, base, {'email': 'one@example.org', 'role': 'reviewer'}).json()
    first.refresh_from_db()
    assert first.revoked_at
    Invitation.objects.filter(pk=original['id']).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert post(admin, base, {'email': 'three@example.org', 'role': 'reader'}).status_code == 201
    assert APIClient().get(ROOT + '/auth/invitations/' + original['invite_url'].rsplit('/', 1)[1]).status_code == 410
