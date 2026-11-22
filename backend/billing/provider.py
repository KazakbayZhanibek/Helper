"""Small HTTPS adapter for Dodo's hosted checkout, customer portal and Standard Webhooks."""
import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request

from django.conf import settings
from django.core.cache import cache
from core.http import DomainError
from core.services import fail


def mode():
    return getattr(settings, 'DODO_MODE', 'test')


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is not None


def configured():
    return (mode() in {'test', 'live'} and bool(getattr(settings, 'DODO_API_KEY', ''))
            and bool(signing_key())
            and identifier(getattr(settings, 'DODO_PRODUCT_ID', ''))
            and identifier(getattr(settings, 'DODO_BUSINESS_ID', ''))
            and bool(getattr(settings, 'PUBLIC_APP_URL', '')))


def signing_key():
    key = getattr(settings, 'DODO_WEBHOOK_SECRET', '')
    try:
        raw = key.removeprefix('whsec_')
        return base64.b64decode(raw + '=' * (-len(raw) % 4), validate=True)
    except (ValueError, binascii.Error):
        return b''


class NoRedirect(HTTPRedirectHandler):
    # The Bearer credential is only valid on the configured provider API host.
    def redirect_request(self, *args):
        return None


def request_json(method, path, body=None, *, timeout=9):
    if not configured():
        fail('BILLING_UNAVAILABLE', 'Приём платежей ещё не подключён.', 503)
    data = None if body is None else json.dumps(body).encode()
    request = Request(f'https://{mode()}.dodopayments.com{path}', data=data, method=method, headers={
        'Authorization': 'Bearer ' + settings.DODO_API_KEY,
        'Content-Type': 'application/json', 'Accept': 'application/json',
    })
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            payload = json.loads(response.read(65536))
            if isinstance(payload, dict):
                return payload
    except (HTTPError, URLError, OSError, ValueError):
        pass
    fail('BILLING_PROVIDER_UNAVAILABLE', 'Платёжный сервис временно недоступен. Попробуйте позже.', 503)


def product_price(product):
    """Display the merchant's catalog base price. Taxes and local offers are settled at checkout."""
    if (product.get('product_id') != settings.DODO_PRODUCT_ID or product.get('business_id') != settings.DODO_BUSINESS_ID
            or product.get('is_recurring') is not True):
        return None
    price = product.get('price')
    if not isinstance(price, dict) or price.get('type') != 'recurring_price':
        return None
    amount, currency = price.get('price'), price.get('currency')
    count, unit = price.get('payment_frequency_count'), price.get('payment_frequency_interval')
    units = {'day': ('день', 'дн.'), 'week': ('неделя', 'нед.'), 'month': ('месяц', 'мес.'), 'year': ('год', 'г.')}
    if (type(amount) is not int or amount < 0 or type(count) is not int or count < 1 or not isinstance(unit, str) or unit not in units
            or not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency)):
        return None
    zero_digits = {'BIF', 'CLP', 'DJF', 'GNF', 'ISK', 'JPY', 'KMF', 'KRW', 'PYG', 'RWF', 'UGX', 'UYI', 'VND', 'VUV', 'XAF', 'XOF', 'XPF'}
    three_digits = {'BHD', 'IQD', 'JOD', 'KWD', 'LYD', 'OMR', 'TND'}
    digits = 0 if currency in zero_digits else 3 if currency in three_digits else 2
    value = format(Decimal(amount).scaleb(-digits), 'f')
    if '.' in value:
        value = value.rstrip('0').rstrip('.')
    interval = units[unit][0] if count == 1 else f'{count} {units[unit][1]}'
    return f"{value.replace('.', ',')} {currency} / {interval}"


def offer():
    if not configured():
        return None
    key = f'billing:offer:{mode()}:{settings.DODO_BUSINESS_ID}:{settings.DODO_PRODUCT_ID}'
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    if not cache.add(key + ':fetching', 1, timeout=8):
        return None
    try:
        product = request_json('GET', '/products/' + settings.DODO_PRODUCT_ID, timeout=3)
        price = product_price(product)
    except DomainError:
        price = None
    finally:
        cache.delete(key + ':fetching')
    cache.set(key, price or '', timeout=300 if price else 20)
    return price


def hosted_url(value):
    if isinstance(value, str) and len(value) <= 2048:
        try:
            url = urlsplit(value)
            if (url.scheme == 'https' and url.hostname and url.hostname.endswith('.dodopayments.com')
                    and url.username is None and url.password is None and url.port in (None, 443)):
                return value
        except ValueError:
            pass
    fail('BILLING_PROVIDER_UNAVAILABLE', 'Платёжный сервис не вернул защищённую ссылку.', 503)


def return_url(org):
    return f"{settings.PUBLIC_APP_URL.rstrip('/')}/o/{org.pk}/billing"


def start_checkout(attempt):
    result = request_json('POST', '/checkouts', {
        'product_cart': [{'product_id': attempt.product_id, 'quantity': 1}],
        'customer': {'email': attempt.payer.email, 'name': attempt.payer.display_name},
        'feature_flags': {'allow_customer_editing_email': False, 'always_create_new_customer': True},
        'metadata': {'recipe_checkout': str(attempt.pk)},
        'return_url': return_url(attempt.organization) + '?checkout=return',
    })
    if not identifier(result.get('session_id')):
        fail('BILLING_PROVIDER_UNAVAILABLE', 'Не удалось открыть оформление подписки.', 503)
    return result['session_id'], hosted_url(result.get('checkout_url'))


def customer_portal(attempt):
    query = urlencode({'return_url': return_url(attempt.organization), 'send_email': 'false'})
    result = request_json('POST', f'/customers/{attempt.customer_id}/customer-portal/session?{query}')
    return hosted_url(result.get('link'))


def verify(raw, headers):
    secret = signing_key()
    if not secret or mode() not in {'test', 'live'} or not getattr(settings, 'DODO_BUSINESS_ID', ''):
        fail('BILLING_UNAVAILABLE', 'Проверка уведомлений оплаты не настроена.', 503)
    event_id = headers.get('webhook-id', '')
    timestamp = headers.get('webhook-timestamp', '')
    signatures = headers.get('webhook-signature', '')
    if (identifier(event_id) and re.fullmatch(r'[0-9]{1,12}', timestamp)
            and abs(time.time() - int(timestamp)) <= 300 and len(signatures) <= 1024):
        signed = event_id.encode() + b'.' + timestamp.encode() + b'.' + raw
        expected = b'v1,' + base64.b64encode(hmac.digest(secret, signed, hashlib.sha256))
        if any(hmac.compare_digest(signature.encode(), expected) for signature in signatures.split()):
            return event_id
    fail('BILLING_BAD_SIGNATURE', 'Не удалось подтвердить уведомление оплаты.', 401)
