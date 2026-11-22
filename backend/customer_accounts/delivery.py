"""Deliver transactional mail outside HTTP requests with a bounded, reclaimable lease."""
import logging
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.db.models import F
from django.utils import timezone

from . import services
from .models import AccountMail

logger = logging.getLogger(__name__)


def message_for(mail):
    if mail.kind == 'invite':
        invite = mail.invitation
        if (not invite or invite.accepted_at or invite.revoked_at or invite.organization.status != 'active'
                or invite.expires_at <= timezone.now()):
            return None
        href = services.public_link('/invite/' + services.invite_token(invite, mailbox=True))
        return ('Приглашение на кухню', f'Вас пригласили на кухню «{invite.organization.name}».\n\n{href}\n\nСсылка действует семь дней.', invite.email)
    user = mail.user
    if not user or not user.is_active or not user.has_usable_password():
        return None
    if mail.kind == 'verify' and mail.identity_state == services.verified_state(user) and not services.is_email_verified(user):
        return ('Подтвердите email в «Рецептах мастера»',
                f'Откройте ссылку, чтобы подтвердить адрес для аккаунта «Рецепты мастера».\n\n{services.verification_link(user)}\n\nСсылка действует 24 часа.', user.email)
    if mail.kind == 'reset' and mail.identity_state == services.reset_state(user):
        href = services.public_link('/reset-password?' + urlencode({'uid': user.pk, 'token': default_token_generator.make_token(user)}))
        return ('Смена пароля в «Рецептах мастера»',
                f'Откройте ссылку, чтобы задать новый пароль.\n\n{href}\n\nЕсли вы не запрашивали смену пароля, просто удалите письмо.', user.email)
    return None


def deliver_one():
    now = timezone.now()
    due = AccountMail.objects.filter(available_at__lte=now)
    key = due.order_by('available_at', 'pk').values_list('pk', flat=True).first()
    if key is None:
        return False
    claim = uuid.uuid4()
    if not due.filter(pk=key).update(claim=claim, available_at=now + timedelta(minutes=3), attempts=F('attempts') + 1):
        return False
    owned = AccountMail.objects.filter(pk=key, claim=claim)
    mail = owned.select_related('user', 'invitation__organization').first()
    if not mail:
        return True
    try:
        # A forgotten request cannot create a fresh reset link days later.
        message = message_for(mail) if mail.created_at > now - timedelta(days=1) else None
        if message:
            subject, body, address = message
            if send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [address], fail_silently=False) != 1:
                raise RuntimeError('Message not accepted')
    except (OSError, ValueError, RuntimeError):
        if mail.attempts < 8:
            delay = timedelta(minutes=min(2 ** mail.attempts, 120))
            owned.update(available_at=timezone.now() + delay, claim=None)
            logger.warning('Account email delivery delayed (task %s, attempt %s).', key, mail.attempts)
        else:
            logger.error('Account email delivery failed (task %s).', key)
            owned.delete()
        return True
    owned.delete()
    return True
