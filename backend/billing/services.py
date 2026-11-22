import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Case, F, Q, When
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.models import Instruction, Membership, Organization
from core.services import fail
from . import provider
from .models import Checkout, LegacyLibrary, WebhookReceipt

PLANS = {
    'free': {'code': 'free', 'name': 'Старт', 'recipe_limit': 5, 'member_limit': 3, 'price_label': 'Бесплатно'},
    'team': {'code': 'team', 'name': 'Команда', 'recipe_limit': 100, 'member_limit': 15, 'price_label': 'Оформление временно недоступно'},
}
TERMINAL = {'cancelled', 'failed', 'expired'}
STATUSES = TERMINAL | {'pending', 'active', 'on_hold', 'past_due', 'paused', 'other_product'}


def subscriptions(org):
    return Checkout.objects.filter(organization=org, mode=provider.mode(), subscription_id__isnull=False)


def preferred_subscriptions():
    # Use the active period first, even if an unrelated, more recent checkout has ended.
    active_end = Case(When(status='active', period_end__gt=timezone.now(), then='period_end'))
    return Checkout.objects.filter(mode=provider.mode(), subscription_id__isnull=False).alias(
        active_end=active_end
    ).order_by(F('active_end').desc(nulls_last=True), '-created_at', '-pk')


def current(org):
    return preferred_subscriptions().filter(organization=org).first()


def entitlement(organization):
    return snapshot(current(organization))


def snapshot(row):
    team = row is not None and row.status == 'active' and row.period_end and row.period_end > timezone.now()
    return {'plan': 'team' if team else 'free', 'status': row.status if row else 'free',
            'period_end': row.period_end.isoformat() if row and row.period_end else None,
            'cancel_at_period_end': bool(row and row.cancel_at_period_end)}


def metered_library(org):
    documents = Instruction.objects.filter(organization=org, lifecycle='active')
    cutoff = LegacyLibrary.objects.filter(organization=org).values_list('upgraded_at', flat=True).first()
    if cutoff:
        documents = documents.filter(Q(created_at__gte=cutoff) | Q(content_kind='recipe')
                                     | Q(working_copy__recipe__isnull=False) | Q(current_version__snapshot__has_key='recipe'))
    return documents


def recipe_count(org):
    return metered_library(org).count()


def converting_legacy(working, recipe):
    if recipe is not None and working.recipe is None and not metered_library(working.organization).filter(pk=working.instruction_id).exists():
        enforce_recipe_limit(working.organization)


def member_count(org):
    return Membership.objects.filter(organization=org, is_active=True, user__is_active=True).count()


def plans(price):
    return [PLANS['free'], {**PLANS['team'], 'price_label': price or PLANS['team']['price_label']}]


def enforce_recipe_limit(org):
    limit = PLANS[entitlement(org)['plan']]['recipe_limit']
    if recipe_count(org) >= limit:
        fail('RECIPE_LIMIT_REACHED', f'Достигнут предел тарифа: {limit} рецептов. Управление тарифом доступно администратору.', 409)


def enforce_member_limit(organization, pending=0):
    """Caller holds the kitchen row lock until the seat or invitation is committed."""
    limit = PLANS[entitlement(organization)['plan']]['member_limit']
    if member_count(organization) + pending >= limit:
        fail('MEMBER_LIMIT_REACHED', f'Достигнут предел тарифа: {limit} участников с учётом приглашений.', 409)


def blocks_checkout(org):
    # An overdue renewal can still collect. Never start a duplicate subscription
    # as a workaround for the old one's outstanding balance or a delayed webhook.
    return subscriptions(org).exclude(status__in=TERMINAL).exists()


def status(member):
    row = current(member.organization)
    admin = member.role == 'admin'
    enabled = provider.configured() and admin
    price = provider.offer()
    return {**snapshot(row), 'usage': {'recipes': recipe_count(member.organization), 'members': member_count(member.organization)},
            'plans': plans(price), 'can_manage': admin, 'mode': provider.mode(),
            'checkout_available': bool(enabled and price and not blocks_checkout(member.organization)),
            'portal_available': bool(enabled and row and row.customer_id and row.payer_id == member.user_id)}


@transaction.atomic
def checkout(member):
    from customer_accounts.services import require_verified_email
    require_verified_email(member.user)
    org = Organization.objects.select_for_update().get(pk=member.organization_id)
    if not provider.configured():
        fail('BILLING_UNAVAILABLE', 'Приём платежей ещё не подключён.', 503)
    if not provider.offer():
        fail('BILLING_PRODUCT_UNAVAILABLE', 'Цена подписки сейчас недоступна. Попробуйте позже.', 503)
    if blocks_checkout(org):
        fail('SUBSCRIPTION_EXISTS', 'У кухни уже есть подписка. Используйте управление оплатой.', 409)
    pending = Checkout.objects.filter(organization=org, mode=provider.mode(), subscription_id__isnull=True).order_by('-created_at').first()
    if pending:
        age = timezone.now() - pending.created_at
        unused = True
        if age >= timedelta(seconds=15):
            # A used checkout link is single-use. Recover from a declined card
            # with a fresh session, but never duplicate a payment still settling.
            remote = provider.request_json('GET', '/checkouts/' + pending.session_id)
            if remote.get('id') != pending.session_id:
                fail('BILLING_PROVIDER_UNAVAILABLE', 'Не удалось проверить предыдущее оформление.', 503)
            unused = not remote.get('payment_id') and not remote.get('payment_status')
            if not unused and remote.get('payment_status') not in {'failed', 'cancelled'}:
                fail('PAYMENT_PROCESSING', 'Предыдущая оплата подтверждается. Обновите статус подписки.', 409)
        if unused and age < timedelta(hours=24):
            if pending.payer_id != member.user_id:
                fail('CHECKOUT_PENDING', 'Другой администратор уже открыл оформление подписки.', 409)
            return pending.checkout_url
    # The organization lock serializes simultaneous clicks. Creating a hosted
    # session does not debit a card. Its link is returned only after commit.
    row = Checkout(organization=org, payer=member.user, mode=provider.mode(), product_id=settings.DODO_PRODUCT_ID)
    row.session_id, row.checkout_url = provider.start_checkout(row)
    row.save()
    return row.checkout_url


def portal(member):
    from customer_accounts.services import require_verified_email
    require_verified_email(member.user)
    row = current(member.organization)
    if not row or not row.customer_id or row.payer_id != member.user_id:
        fail('PORTAL_UNAVAILABLE', 'Платёжный кабинет доступен администратору, оформившему подписку.', 403)
    return provider.customer_portal(row)


def refresh(member):
    """Follow server-owned IDs to Dodo's actual subscription if a delivery was delayed."""
    now = timezone.now()
    with transaction.atomic():
        org = Organization.objects.select_for_update().get(pk=member.organization_id)
        row = Checkout.objects.filter(organization=org, mode=provider.mode(), payer=member.user).order_by('-created_at').first()
        if not row or not provider.configured() or (row.last_checked_at and row.last_checked_at > now - timedelta(seconds=20)):
            return status(member)
        row.last_checked_at = now
        row.save(update_fields=['last_checked_at'])
    subscription_id = row.subscription_id
    if not subscription_id:
        session = provider.request_json('GET', '/checkouts/' + row.session_id)
        payment_id = session.get('payment_id')
        if session.get('id') != row.session_id or session.get('payment_status') != 'succeeded' or not provider.identifier(payment_id):
            return status(member)
        payment = provider.request_json('GET', '/payments/' + payment_id)
        ids = payment.get('subscription_ids') or [payment.get('subscription_id')]
        if (payment.get('business_id') != settings.DODO_BUSINESS_ID or payment.get('payment_id') != payment_id
                or payment.get('checkout_session_id') != row.session_id or payment.get('status') != 'succeeded'
                or not isinstance(ids, list) or len(ids) != 1 or not provider.identifier(ids[0])):
            return status(member)
        subscription_id = ids[0]
    data = provider.request_json('GET', '/subscriptions/' + subscription_id)
    if data.get('subscription_id') != subscription_id:
        return status(member)
    if row.subscription_id is None:
        metadata = data.get('metadata')
        if not isinstance(metadata, dict) or metadata.get('recipe_checkout') != str(row.pk) or data.get('product_id') != row.product_id:
            return status(member)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=row.organization_id)
        row.refresh_from_db()
        apply_subscription(row, data, now)
    return status(member)


def aware_date(value):
    try:
        date = parse_datetime(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return date if date and timezone.is_aware(date) else None


@transaction.atomic
def process_event(event_id, event):
    if not isinstance(event, dict) or not isinstance(event.get('type'), str):
        fail('INVALID_BILLING_EVENT', 'Некорректный формат уведомления.', 400)
    if event.get('business_id') != getattr(settings, 'DODO_BUSINESS_ID', ''):
        fail('WRONG_BILLING_BUSINESS', 'Уведомление относится к другому магазину.', 403)
    if not event['type'].startswith('subscription.'):
        return
    data = event.get('data')
    if not isinstance(data, dict) or not provider.identifier(data.get('subscription_id')):
        fail('INVALID_BILLING_EVENT', 'Некорректные данные подписки.', 400)
    mode = provider.mode()
    subscription_id = data['subscription_id']
    rows = Checkout.objects.filter(mode=mode)
    row = rows.filter(subscription_id=subscription_id).first()
    if row is None:
        metadata = data.get('metadata')
        token = metadata.get('recipe_checkout') if isinstance(metadata, dict) else None
        try:
            checkout_id = uuid.UUID(str(token))
        except (TypeError, ValueError):
            return  # Another product or integration in the merchant account.
        row = rows.filter(pk=checkout_id).first()
        if row is None or data.get('product_id') != row.product_id:
            return
    Organization.objects.select_for_update().get(pk=row.organization_id)
    row.refresh_from_db()
    _, is_new = WebhookReceipt.objects.get_or_create(mode=mode, event_id=event_id)
    if not is_new:
        return
    apply_subscription(row, data, aware_date(event.get('timestamp')))


def apply_subscription(row, data, occurred):
    subscription_id = data['subscription_id']
    if row.subscription_id and row.subscription_id != subscription_id:
        fail('BILLING_LINK_CONFLICT', 'Оформление уже связано с другой подпиской.', 409)
    period_end = aware_date(data.get('next_billing_date'))
    customer = data.get('customer')
    state = data.get('status')
    if (not occurred or not isinstance(state, str) or state not in STATUSES - {'other_product'}
            or not isinstance(customer, dict) or not provider.identifier(customer.get('customer_id'))
            or type(data.get('cancel_at_next_billing_date')) is not bool
            or not provider.identifier(data.get('product_id')) or (state == 'active' and not period_end)):
        fail('INVALID_BILLING_EVENT', 'Не хватает информации о состоянии подписки.', 400)
    if row.event_time and occurred < row.event_time:
        return
    if row.customer_id and row.customer_id != customer['customer_id']:
        fail('BILLING_LINK_CONFLICT', 'Покупатель подписки изменился.', 409)
    if data['product_id'] != row.product_id:
        state = 'other_product'
    if occurred == row.event_time and state == 'active' and row.status != 'active':
        return  # A restriction wins over an equally dated activation.
    row.subscription_id, row.customer_id = subscription_id, customer['customer_id']
    row.status, row.period_end, row.event_time = state, period_end, occurred
    row.cancel_at_period_end = data['cancel_at_next_billing_date']
    row.checkout_url = ''
    row.save(update_fields=['subscription_id', 'customer_id', 'status', 'period_end', 'event_time',
                            'cancel_at_period_end', 'checkout_url'])
