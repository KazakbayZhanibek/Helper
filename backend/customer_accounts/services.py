import ipaddress
import unicodedata
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.crypto import salted_hmac

from core import services as core
from core.models import Membership, Organization, User
from .models import AccountMail, AccountRateLimit, EmailIdentity, Invitation


def is_email_verified(user):
    identity = EmailIdentity.objects.filter(user=user).values('email', 'verified_at').first()
    return identity is None or bool(identity['verified_at'] and identity['email'] == user.email)


def require_verified_email(user):
    if not is_email_verified(user):
        core.fail('EMAIL_VERIFICATION_REQUIRED', 'Подтвердите email по ссылке из письма.', 403)


def email_field(data):
    email = core.text_field(data, 'email', 1, 254).lower()
    try:
        validate_email(email)
    except ValidationError:
        core.fail('VALIDATION_ERROR', 'Проверьте адрес электронной почты.', fields={'email': ['Введите действительный email.']})
    return email


def name_field(data, key):
    value = core.text_field(data, key, 1, 120)
    if any(unicodedata.category(char) in {'Cc', 'Zl', 'Zp'} for char in value):
        core.fail('VALIDATION_ERROR', 'Введите имя в одну строку.', fields={key: ['Уберите переносы строк и управляющие символы.']})
    return value


def password_field(data, user):
    password = data.get('password')
    if not isinstance(password, str) or len(password) > 256 or '\x00' in password or any(0xd800 <= ord(c) <= 0xdfff for c in password):
        core.fail('VALIDATION_ERROR', 'Проверьте пароль.', fields={'password': ['Введите пароль длиной до 256 символов.']})
    try:
        validate_password(password, user)
    except ValidationError as error:
        core.fail('VALIDATION_ERROR', 'Выберите другой пароль.', fields={'password': list(error.messages)})
    return password


def public_link(path):
    return settings.APP_PUBLIC_URL.rstrip('/') + path


def invite_token(invite, *, mailbox=False):
    # The copyable link shown to an admin grants precisely the kitchen invitation.
    # The separately signed prefix is sent only to the actual recipient.
    value = ('email.' if mailbox else '') + invite.pk.hex
    return signing.Signer(salt='customer_accounts.invite.v1', sep='.').sign(value)


def invite_details(invite):
    return {'id': str(invite.pk), 'email': invite.email, 'role': invite.role, 'expires_at': core.utc(invite.expires_at)}


def decode_invitation(token):
    try:
        value = signing.Signer(salt='customer_accounts.invite.v1', sep='.').unsign(token)
        return uuid.UUID(value.removeprefix('email.')), value.startswith('email.')
    except (ValueError, signing.BadSignature):
        core.fail('INVITATION_INVALID', 'Приглашение недействительно или истекло.', 410)


def get_invitation(token, *, lock=False):
    pk, mailbox = decode_invitation(token)
    query = Invitation.objects.select_related('organization')
    invite = query.filter(pk=pk).first()
    if invite and lock:
        # The same lock serializes invitation creation, removal and plan changes.
        org = Organization.objects.select_for_update().get(pk=invite.organization_id)
        invite = Invitation.objects.select_for_update().get(pk=pk)
        invite.organization = org
    if not invite or invite.revoked_at or invite.expires_at <= timezone.now() or invite.organization.status != 'active':
        core.fail('INVITATION_INVALID', 'Приглашение недействительно или истекло.', 410)
    return invite, mailbox


def verified_state(user):
    return salted_hmac('customer_accounts.email.v1', f'{user.pk}:{user.email}', algorithm='sha256').hexdigest()


def verification_link(user):
    token = signing.TimestampSigner(salt='customer_accounts.confirm.v1').sign(verified_state(user))
    return public_link('/verify-email?' + urlencode({'uid': user.pk, 'token': token}))


def reset_state(user):
    return salted_hmac('customer_accounts.reset.v1', f'{user.pk}:{user.email}:{user.password}:{user.last_login}', algorithm='sha256').hexdigest()


def queue_verification(user):
    AccountMail.objects.create(kind='verify', user=user, identity_state=verified_state(user))


def create_user(email, data, *, mailbox=False):
    name = name_field(data, 'display_name')
    candidate = User(email=email, display_name=name)
    password = password_field(data, candidate)
    # Publicly created identities can never acquire trusted legacy status.
    try:
        with transaction.atomic():
            user = User.objects.create_user(email, password, display_name=name)
    except IntegrityError:
        if User.objects.filter(email__iexact=email).exists():
            core.fail('EMAIL_IN_USE', 'Этот адрес уже зарегистрирован. Войдите или восстановите пароль.', 409,
                      fields={'email': ['Этот адрес уже зарегистрирован.']})
        raise
    EmailIdentity.objects.create(user=user, email=email, verified_at=timezone.now() if mailbox else None)
    if not mailbox:
        queue_verification(user)
    return user


def client_address(request):
    address = request.META.get('REMOTE_ADDR', '')
    if getattr(settings, 'SECURE_PROXY_SSL_HEADER', None):
        forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '').rsplit(',', 1)[-1].strip()
        try:
            address = str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return address


def allowance(bucket, value, maximum, minutes):
    """One DB counter is shared by API processes. Fixed windows expire in bounded batches."""
    now = timezone.now()
    bucket_seconds = minutes * 60
    window = int(now.timestamp()) // bucket_seconds
    key = salted_hmac('customer_accounts.limit.v1', f'{bucket}:{window}:{value}', algorithm='sha256').hexdigest()
    old_keys = list(AccountRateLimit.objects.filter(expires_at__lte=now).values_list('pk', flat=True)[:32])
    if old_keys:
        AccountRateLimit.objects.filter(pk__in=old_keys, expires_at__lte=now).delete()
    AccountRateLimit.objects.get_or_create(key=key, defaults={'expires_at': now + timedelta(minutes=minutes)})
    return bool(AccountRateLimit.objects.filter(pk=key, count__lt=maximum).update(count=F('count') + 1))


def rate_limit(request, bucket, maximum, minutes):
    if not allowance(bucket, client_address(request), maximum, minutes):
        core.fail('RATE_LIMITED', 'Слишком много попыток. Попробуйте позже.', 429)


def locked_admin(user, org_id):
    member = core.membership(user, org_id, {'admin'})
    Organization.objects.select_for_update().get(pk=member.organization_id)
    return core.membership(user, org_id, {'admin'})


def link_user(data):
    try:
        if not isinstance(data.get('uid'), str) or not isinstance(data.get('token'), str) or len(data['token']) > 256:
            return None
        pk = uuid.UUID(data['uid'])
    except ValueError:
        return None
    return User.objects.select_for_update().filter(pk=pk, is_active=True).first()
