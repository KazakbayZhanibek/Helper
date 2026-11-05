"""Atomic, fail-closed limits for outbound AI requests.

Unknown dispatched calls retain their entire reservation. Retries cannot replay a
call_key, even in a new worker attempt. Provider receipts settle independently of
an authoring job's lease, cancellation, or output validation.
"""
from datetime import timezone as datetime_timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import threading
import random
import time
from urllib.parse import urlsplit
from django.conf import settings
from django.db import OperationalError, connections, transaction
from django.db.models import F, Sum, Q
from django.utils import timezone

from ai_drafts.providers import ProviderFailure, OPENROUTER_ENDPOINT, OPENROUTER_BUDGET_MODEL, is_free_openrouter_model
from .models import AIUsageReservation, BudgetAccount

PRICING_VERSION = 'openai-gpt-5.4-mini-2026-11-01'
MAX_TEXT_BYTES = 32768
PROTOCOL_TOKEN_ALLOWANCE = 2048
MAX_OUTPUT_TOKENS = 8192
MAX_IMAGE_TOKENS = 1080  # ceil(960/32)^2 * 1.2, high detail, never upscaled.
# Official rates checked 2026-11-01. Unknown models/endpoints have no cash estimate.
# https://developers.openai.com/api/docs/models/gpt-5.4-mini
# https://developers.openai.com/api/docs/guides/images-vision
PRICES = {'gpt-5.4-mini': (3, 4, 9, 2)}  # USD micros/token as exact fractions.


def _setting(name, default):
    value = getattr(settings, name, default)
    return max(0, value) if type(value) is int else default


def estimate_call(provider, model, frames_count=12):
    if type(frames_count) is not int or not 0 <= frames_count <= 12:
        raise ProviderFailure('AI_BUDGET_INPUT_UNBOUNDED', 'Размер запроса выходит за проверенные границы бюджета.')
    if provider in {'codex', 'ollama'}:
        return {'unit': 'calls', 'max_units': 1, 'pricing_version': 'subscription-call-quota-v1' if provider == 'codex' else 'local-call-quota-v1'}
    if provider == 'openrouter' and is_free_openrouter_model(model):
        return {'unit': 'calls', 'max_units': 1, 'pricing_version': 'openrouter-free-only-v1'}
    if provider == 'openrouter' and model == OPENROUTER_BUDGET_MODEL:
        # 960px JPEGs, aspect <=4 for images >384px: conservative allowance
        # beyond Gemini's 258 tokens/tile. Text is bounded by UTF-8 bytes.
        # https://ai.google.dev/gemini-api/docs/tokens
        # https://openrouter.ai/google/gemini-2.5-flash-lite (2026-11-05)
        tokens = MAX_TEXT_BYTES + PROTOCOL_TOKEN_ALLOWANCE + frames_count * 4096
        amount = (tokens + 9) // 10 + (MAX_OUTPUT_TOKENS * 2 + 4) // 5
        return {'unit': 'usd_micros', 'max_units': amount + 1000,
                'pricing_version': 'openrouter-gemini-2.5-flash-lite-2026-11-05',
                'max_input_tokens': tokens, 'max_output_tokens': MAX_OUTPUT_TOKENS,
                'input_rate_numerator': 1, 'input_rate_denominator': 10,
                'output_rate_numerator': 2, 'output_rate_denominator': 5}
    if provider != 'openai' or model not in PRICES:
        raise ProviderFailure('AI_PRICING_UNAVAILABLE', 'Для этой модели ещё не настроен проверенный расчёт расходов. Платный запрос не отправлен.')
    input_tokens = MAX_TEXT_BYTES + PROTOCOL_TOKEN_ALLOWANCE + frames_count * MAX_IMAGE_TOKENS
    input_num, input_den, output_num, output_den = PRICES[model]
    max_units = (input_tokens * input_num + input_den - 1) // input_den + (MAX_OUTPUT_TOKENS * output_num + output_den - 1) // output_den
    return {'unit': 'usd_micros', 'max_units': max_units, 'pricing_version': PRICING_VERSION, 'max_input_tokens': input_tokens, 'max_output_tokens': MAX_OUTPUT_TOKENS, 'input_rate_numerator': input_num, 'input_rate_denominator': input_den, 'output_rate_numerator': output_num, 'output_rate_denominator': output_den}


def budget_capabilities(provider, model):
    result = {'mode': 'unavailable', 'currency': None, 'default_max_calls': min(_setting('AI_BUDGET_DEFAULT_CALLS', 30), _setting('AI_BUDGET_MAX_CALLS', 60)), 'max_calls': _setting('AI_BUDGET_MAX_CALLS', 60), 'default_max_cost_microusd': None, 'max_cost_microusd': None, 'call_upper_bound_microusd': None, 'pricing_version': None}
    try:
        estimate = estimate_call(provider, model)
    except ProviderFailure:
        return result
    result['pricing_version'] = estimate['pricing_version']
    if estimate['unit'] == 'calls':
        result['mode'] = {'codex': 'subscription_calls', 'ollama': 'local_calls', 'openrouter': 'free_calls'}[provider]
    else:
        result.update(mode='usd', currency='USD', default_max_cost_microusd=min(_setting('AI_BUDGET_DEFAULT_MICROUSD', 2000000), _setting('AI_BUDGET_MAX_MICROUSD', 5000000)), max_cost_microusd=_setting('AI_BUDGET_MAX_MICROUSD', 5000000), call_upper_bound_microusd=estimate['max_units'])
    return result


def _limits(session):
    config = getattr(session, 'analysis_budget', {}) or {}
    if not isinstance(config, dict):
        config = {}
    calls = config.get('max_calls', getattr(session, 'max_ai_calls', _setting('AI_BUDGET_DEFAULT_CALLS', 30)))
    cash = config.get('max_cost_microusd', _setting('AI_BUDGET_DEFAULT_MICROUSD', 2000000))
    if type(calls) is not int or type(cash) is not int or calls < 0 or cash < 0:
        raise ProviderFailure('AI_BUDGET_INVALID', 'Некорректный лимит анализа.')
    return min(calls, _setting('AI_BUDGET_MAX_CALLS', 60)), min(cash, _setting('AI_BUDGET_MAX_MICROUSD', 5000000))


def _accounts(session, unit):
    now = timezone.now().astimezone(datetime_timezone.utc)
    day, month = now.strftime('%Y-%m-%d'), now.strftime('%Y-%m')
    calls, cash = _limits(session)
    if unit == 'calls':
        caps = [calls, _setting('AI_BUDGET_ORG_DAY_CALLS', 100), _setting('AI_BUDGET_ORG_MONTH_CALLS', 2000), _setting('AI_BUDGET_PLATFORM_DAY_CALLS', 500)]
    else:
        caps = [cash, _setting('AI_BUDGET_ORG_DAY_MICROUSD', 10000000), _setting('AI_BUDGET_ORG_MONTH_MICROUSD', 100000000), _setting('AI_BUDGET_PLATFORM_DAY_MICROUSD', 50000000)]
    return sorted(zip([f'{unit}:session:{session.id}', f'{unit}:org-day:{session.organization_id}:{day}', f'{unit}:org-month:{session.organization_id}:{month}', f'{unit}:platform-day:{day}'], caps))


def _blocked():
    return ProviderFailure('AI_BUDGET_EXCEEDED', 'Лимит расходов или обращений к ИИ достигнут. Уже полученные результаты сохранены; новый запрос не отправлен.')


def _provider_accounts(provider, model):
    if provider != 'openrouter':
        return []
    if model == OPENROUTER_BUDGET_MODEL:
        return [('usd_micros:openrouter-total:v1', _setting('OPENROUTER_TOTAL_BUDGET_MICROUSD', 0), 'usd_micros')]
    day = timezone.now().astimezone(datetime_timezone.utc).strftime('%Y-%m-%d')
    return [(f'calls:openrouter-free-day:{day}', min(50, _setting('OPENROUTER_FREE_DAY_CALLS', 50)), 'calls')]


def preflight(session, baseline_count, provider=None):
    """Read-only quote; per-call atomic reservation remains authoritative."""
    if provider is None:
        from ai_drafts.providers import endpoint_configuration
        config = endpoint_configuration(session.organization)
        provider_name, model = config['provider'], config['model']
    else:
        provider_name, model = provider.provider, provider.model
    estimate = estimate_call(provider_name, model)
    if not getattr(settings, 'AI_BUDGET_ENABLED', True):
        raise _blocked()
    if type(baseline_count) is not int or baseline_count < 1:
        raise ProviderFailure('AI_BUDGET_INVALID', 'Неверное число фрагментов.')
    requirements = [(key, limit, baseline_count) for key, limit in _accounts(session, 'calls')]
    requirements += [(key, limit, baseline_count * (estimate['max_units'] if unit == 'usd_micros' else 1))
                     for key, limit, unit in _provider_accounts(provider_name, model)]
    if estimate['unit'] == 'usd_micros':
        requirements += [(key, limit, baseline_count * estimate['max_units']) for key, limit in _accounts(session, 'usd_micros')]
    for key, limit, amount in requirements:
        account = BudgetAccount.objects.filter(pk=key).first()
        if amount > limit or (account and (account.blocked or account.spent + account.reserved + amount > limit)):
            raise _blocked()
    return estimate


def _reserve_once(provider, *, job, session, call_key, frames_count=12):
    if not getattr(settings, 'AI_BUDGET_ENABLED', True):
        raise _blocked()
    estimate = estimate_call(provider.provider, provider.model, frames_count)
    if provider.provider == 'openai' and getattr(provider, 'endpoint', '') != 'https://api.openai.com/v1':
        raise ProviderFailure('AI_PRICING_UNAVAILABLE', 'Расчёт расходов доступен только для проверенного адреса OpenAI API.')
    if provider.provider == 'openrouter' and getattr(provider, 'endpoint', '') != OPENROUTER_ENDPOINT:
        raise ProviderFailure('AI_PRICING_UNAVAILABLE', 'Проверенный тариф доступен только на адресе OpenRouter API.')
    if provider.provider == 'ollama':
        try:
            endpoint = urlsplit(getattr(provider, 'endpoint', ''))
            if endpoint.scheme not in {'http', 'https'} or endpoint.hostname not in {'localhost', '127.0.0.1', '::1'} or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                raise ValueError()
        except ValueError:
            raise ProviderFailure('AI_PRICING_UNAVAILABLE', 'Лимит локальных обращений разрешён только для Ollama на этом компьютере.') from None
    if not isinstance(call_key, str) or not 1 <= len(call_key) <= 160:
        raise ProviderFailure('AI_BUDGET_INVALID', 'Некорректный идентификатор обращения.')
    if session.organization_id != job.organization_id:
        raise ProviderFailure('AI_BUDGET_INVALID', 'Организация бюджета не соответствует заданию.')
    specs = [(key, cap, 1, 'calls') for key, cap in _accounts(session, 'calls')]
    specs += [(key, cap, estimate['max_units'] if unit == 'usd_micros' else 1, unit)
              for key, cap, unit in _provider_accounts(provider.provider, provider.model)]
    if estimate['unit'] == 'usd_micros':
        specs += [(key, cap, estimate['max_units'], 'usd_micros') for key, cap in _accounts(session, 'usd_micros')]
    try:
        with transaction.atomic():
            # Stable account order plus conditional arithmetic prevents concurrent
            # overdraw on both PostgreSQL and SQLite (where row locks are absent).
            for key, cap, amount, unit in sorted(specs):
                BudgetAccount.objects.get_or_create(key=key, defaults={'unit': unit})
                updated = BudgetAccount.objects.filter(pk=key, blocked=False, spent__lte=cap - amount - F('reserved')).update(reserved=F('reserved') + amount)
                if updated != 1:
                    raise _blocked()
            # All callers of a logical session share an account lock before this
            # check. A different worker attempt never replays a dispatched call.
            if AIUsageReservation.objects.filter(job_id=job.id, call_key=call_key).exclude(state='released').exists():
                raise ProviderFailure('AI_CALL_OUTCOME_UNKNOWN', 'Этот запрос уже отправлялся. Автоматический повтор заблокирован, чтобы исключить повторный расход.')
            return AIUsageReservation.objects.create(organization_id=job.organization_id, session_id=session.id, job_id=job.id, attempt=max(1, getattr(job, 'attempts', 1)), call_key=call_key, provider=provider.provider, model=provider.model, unit=estimate['unit'], pricing_version=estimate['pricing_version'], pricing_snapshot=estimate, max_units=estimate['max_units'], account_keys=[{'key': key, 'amount': amount, 'unit': unit} for key, cap, amount, unit in specs])
    except OperationalError:
        raise ProviderFailure('AI_BUDGET_BUSY', 'Бюджет занят другим заданием. Новый запрос не отправлен.', True) from None


def reserve(provider, *, job, session, call_key, frames_count=12):
    # SQLite can reject both contenders upgrading a read transaction. A short,
    # bounded retry is safe here: the entire failed transaction was rolled back
    # and no transport has started. Never retry an outbound request here.
    for attempt in range(4):
        try:
            return _reserve_once(provider, job=job, session=session, call_key=call_key, frames_count=frames_count)
        except ProviderFailure as error:
            if error.code != 'AI_BUDGET_BUSY' or attempt == 3:
                raise
            time.sleep(0.01 * (attempt + 1) + random.uniform(0, 0.02))


def mark_dispatched(reservation):
    if AIUsageReservation.objects.filter(pk=reservation.pk, state='reserved').update(state='dispatched', dispatched_at=timezone.now()) != 1:
        raise ProviderFailure('AI_CALL_OUTCOME_UNKNOWN', 'Запрос уже отправлялся; повтор не выполнен.')


def mark_unknown(reservation):
    AIUsageReservation.objects.filter(pk=reservation.pk, state__in=['reserved', 'dispatched']).update(state='unknown')


def release_unstarted(reservation):
    with transaction.atomic():
        current = AIUsageReservation.objects.select_for_update().get(pk=reservation.pk)
        if current.state != 'reserved':
            return
        # Conditional state transition makes duplicate compensation harmless.
        if AIUsageReservation.objects.filter(pk=current.pk, state='reserved').update(state='released', settled_at=timezone.now()) != 1:
            return
        for spec in sorted(current.account_keys, key=lambda item: item['key']):
            BudgetAccount.objects.filter(pk=spec['key']).update(reserved=F('reserved') - spec['amount'])


def _usage(value):
    if not isinstance(value, dict):
        return None
    prompt, completion = value.get('prompt_tokens'), value.get('completion_tokens')
    if any(type(v) is not int or v < 0 or v > 10000000 for v in [prompt, completion]):
        return None
    return {'prompt_tokens': prompt, 'completion_tokens': completion}


def settle(reservation, usage=None, request_id=''):
    """Commit safe receipt before model content parsing. Missing usage stays held."""
    safe = _usage(usage)
    reported_cost = None
    if reservation.provider == 'openrouter' and isinstance(usage, dict):
        try:
            value = usage.get('cost')
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ValueError()
            cost = Decimal(str(value))
            if not cost.is_finite() or cost < 0 or cost > 1000000:
                raise ValueError()
            reported_cost = int((cost * 1000000).to_integral_value(rounding=ROUND_CEILING))
            safe = {**(safe or {}), 'cost_microusd': reported_cost}
        except (ValueError, InvalidOperation):
            pass
        model = usage.get('resolved_model')
        if isinstance(model, str) and 1 <= len(model) <= 200 and all(c.isalnum() or c in '/:._-' for c in model):
            safe = {**(safe or {}), 'resolved_model': model}
    if reservation.unit == 'usd_micros' and (safe is None or (reservation.provider == 'openrouter' and reported_cost is None)):
        mark_unknown(reservation)
        return
    with transaction.atomic():
        current = AIUsageReservation.objects.select_for_update().get(pk=reservation.pk)
        if current.state not in {'dispatched', 'unknown'}:
            return
        actual = 1
        if current.unit == 'usd_micros' and current.provider == 'openrouter':
            actual = reported_cost
        elif current.unit == 'usd_micros':
            names = ['input_rate_numerator', 'input_rate_denominator', 'output_rate_numerator', 'output_rate_denominator']
            rates = [current.pricing_snapshot.get(name) for name in names]
            if any(type(rate) is not int or rate <= 0 for rate in rates):
                AIUsageReservation.objects.filter(pk=current.pk, state__in=['dispatched', 'unknown']).update(state='unknown')
                return
            input_num, input_den, output_num, output_den = rates
            actual = (safe['prompt_tokens'] * input_num + input_den - 1) // input_den + (safe['completion_tokens'] * output_num + output_den - 1) // output_den
        if AIUsageReservation.objects.filter(pk=current.pk, state__in=['dispatched', 'unknown']).update(state='settled', actual_units=actual, usage=safe or {}, provider_request_id=request_id[:200] if isinstance(request_id, str) else '', settled_at=timezone.now()) != 1:
            return
        overrun = actual > current.max_units
        for spec in sorted(current.account_keys, key=lambda item: item['key']):
            amount = actual if spec['unit'] == current.unit else 1
            BudgetAccount.objects.filter(pk=spec['key']).update(reserved=F('reserved') - spec['amount'], spent=F('spent') + amount, **({'blocked': True} if overrun else {}))


def budget_snapshot(session):
    calls, cash = _limits(session)
    reservations = AIUsageReservation.objects.filter(session_id=session.id, organization_id=session.organization_id).exclude(state='released')
    values = reservations.filter(unit='usd_micros').aggregate(reserved=Sum('max_units', filter=~Q(state='settled')), actual=Sum('actual_units'))
    units = set(reservations.values_list('unit', flat=True))
    from ai_drafts.providers import endpoint_configuration
    configured = endpoint_configuration(session.organization) if hasattr(session, 'organization') else {}
    provider_names = set(reservations.values_list('provider', flat=True))
    active_providers = provider_names or {configured.get('provider')}
    paid_config = configured.get('provider') == 'openai' or (configured.get('provider') == 'openrouter' and configured.get('model') == OPENROUTER_BUDGET_MODEL)
    mode = ('usd' if 'usd_micros' in units or (not units and paid_config) else
            'free_calls' if 'openrouter' in active_providers else
            'local_calls' if 'ollama' in active_providers else 'subscription_calls')
    return {'mode': mode, 'currency': 'USD' if mode == 'usd' else None, 'max_calls': calls, 'calls_used': reservations.count(), 'max_cost_microusd': cash if mode == 'usd' else None, 'reserved_microusd': (values['reserved'] or 0) if mode == 'usd' else None, 'spent_microusd': (values['actual'] or 0) if mode == 'usd' else None, 'unknown_calls': reservations.filter(state__in=['unknown', 'dispatched']).count()}


def execute_budgeted_call(provider, *, job, context, call_key, invoke, session=None, frames_count=0):
    session = session or job
    reservation = reserve(provider, job=job, session=session, call_key=call_key, frames_count=frames_count)
    try:
        getattr(context, 'check_dispatch', context.check)()
    except BaseException:
        release_unstarted(reservation)
        raise
    mark_dispatched(reservation)
    # The callback runs in the HTTP worker thread. It must not depend on the job
    # lease: a late response is still billable even after authoring cancellation.
    def receipt(usage, request_id=''):
        try:
            settle(reservation, usage, request_id)
        except OperationalError:
            # The full reservation remains held; never refund on accounting error.
            return
    provider._usage_recorder = receipt
    owner_thread = threading.get_ident()
    def run():
        try:
            result = invoke()
            if provider.provider in {'codex', 'ollama'}:
                receipt(None)
            return result
        finally:
            # Close only the connection owned by this background thread. Codex
            # runs synchronously on the job connection and must keep it intact.
            if threading.get_ident() != owner_thread:
                connections['default'].close()
    try:
        result = run() if provider.provider == 'codex' else context.call(run, timeout=100)
        mark_unknown(reservation)  # no-op after successful settlement
        return result
    except BaseException:
        mark_unknown(reservation)
        raise
    finally:
        # The late background invocation retains this callback on its provider.
        pass


def execute_budgeted(provider, *, job, session, frames, context, call_key):
    return execute_budgeted_call(provider, job=job, session=session, context=context, call_key=call_key, frames_count=len(frames), invoke=lambda: provider.generate(language=session.language, mode=session.mode, frames=frames, context=context if provider.provider == 'codex' else None))
