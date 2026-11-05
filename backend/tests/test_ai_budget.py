import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from PIL import Image
from django.db import close_old_connections, connections

from ai_budget import services
from ai_budget.models import AIUsageReservation, BudgetAccount
from ai_drafts.providers import ProviderFailure, enforce_budget_body
from core.models import Organization

pytestmark = pytest.mark.django_db


def fixture(org, *, provider='codex', calls=30, cash=2000000):
    session = SimpleNamespace(id=uuid.uuid4(), organization_id=org.id, organization=org, analysis_budget={'max_calls': calls, 'max_cost_microusd': cash}, language='ru', mode='upload')
    job = SimpleNamespace(id=uuid.uuid4(), attempts=1, organization_id=org.id)
    ai = SimpleNamespace(provider=provider, model='gpt-5.4-mini' if provider == 'openai' else 'gpt-6-luna', endpoint='https://api.openai.com/v1')
    return session, job, ai


def ctx():
    return SimpleNamespace(check=lambda: None, call=lambda fn, timeout: fn())


def test_known_price_bound_and_unknown_model_fail_closed():
    quote = services.estimate_call('openai', 'gpt-5.4-mini', 12)
    assert quote['max_units'] == 72696
    assert quote['max_input_tokens'] == 47776
    assert services.estimate_call('codex', 'gpt-6-luna')['unit'] == 'calls'
    with pytest.raises(ProviderFailure, match='AI_PRICING_UNAVAILABLE'):
        services.estimate_call('openai', 'gpt-4o')
    with pytest.raises(ProviderFailure, match='AI_PRICING_UNAVAILABLE'):
        services.estimate_call('nvidia', 'a-model')


def test_budget_kill_switch_never_sends(team, settings):
    session, job, provider = fixture(team.org)
    settings.AI_BUDGET_ENABLED = False
    with pytest.raises(ProviderFailure, match='AI_BUDGET_EXCEEDED'):
        services.execute_budgeted_call(provider, session=session, job=job, context=ctx(), call_key='first', invoke=lambda: pytest.fail('sent'))
    assert AIUsageReservation.objects.count() == 0


def test_subscription_quota_has_no_cash_price(team):
    session, job, provider = fixture(team.org, calls=1)
    assert services.execute_budgeted_call(provider, session=session, job=job, context=ctx(), call_key='first', invoke=lambda: {'ok': True}) == {'ok': True}
    reservation = AIUsageReservation.objects.get()
    assert reservation.state == 'settled'
    assert reservation.unit == 'calls' and reservation.actual_units == 1
    snapshot = services.budget_snapshot(session)
    assert snapshot['spent_microusd'] is None and snapshot['currency'] is None
    with pytest.raises(ProviderFailure, match='AI_BUDGET_EXCEEDED'):
        services.execute_budgeted_call(provider, session=session, job=job, context=ctx(), call_key='second', invoke=lambda: pytest.fail('sent'))


def test_cash_reservation_and_exact_usage_receipt(team):
    session, job, provider = fixture(team.org, provider='openai')
    def invoke():
        provider._usage_recorder({'prompt_tokens': 1000, 'completion_tokens': 100}, 'safe-id')
        return {'result': 'ok'}
    services.execute_budgeted_call(provider, session=session, job=job, context=ctx(), call_key='first', invoke=invoke, frames_count=12)
    row = AIUsageReservation.objects.get()
    assert row.actual_units == 1200 and row.state == 'settled'
    assert row.provider_request_id == 'safe-id'
    for account in BudgetAccount.objects.all():
        assert account.reserved == 0
        assert account.spent == (1 if account.unit == 'calls' else 1200)


def test_invalid_response_still_settled_before_validation(team, settings, monkeypatch):
    from visual_drafts.provider import VisualProvider
    settings.STEP_DRAFT_BACKEND = 'openai'
    settings.STEP_DRAFT_MODEL = 'gpt-5.4-mini'
    settings.STEP_DRAFT_ENDPOINT = 'https://api.openai.com/v1'
    settings.STEP_DRAFT_API_KEY = 'fixture-not-a-key'
    settings.EXTERNAL_AI_ENABLED = True
    team.org.external_ai_enabled = True
    session, job, _ = fixture(team.org, provider='openai')
    provider = VisualProvider(team.org)
    buf = io.BytesIO()
    Image.new('RGB', (80, 60), 'red').save(buf, format='JPEG')
    frames = [{'id': str(uuid.uuid4()), 'sequence': index, 'timestamp_ms': index * 2000, 'bytes': buf.getvalue()} for index in [1, 2]]
    monkeypatch.setattr('visual_drafts.provider.json_request', lambda *args, **kwargs: {'id': 'receipt', 'usage': {'prompt_tokens': 1234, 'completion_tokens': 80}, 'choices': [{'finish_reason': 'stop', 'message': {'content': '{invalid'}}]})
    with pytest.raises(ProviderFailure, match='MODEL_OUTPUT_INVALID'):
        services.execute_budgeted(provider, session=session, job=job, context=ctx(), call_key='first', frames=frames)
    row = AIUsageReservation.objects.get()
    assert row.state == 'settled' and row.usage['prompt_tokens'] == 1234


def test_timeout_keeps_entire_reservation_and_blocks_retry(team):
    session, job, provider = fixture(team.org, provider='openai')
    def fail():
        raise TimeoutError()
    with pytest.raises(TimeoutError):
        services.execute_budgeted_call(provider, session=session, job=job, context=ctx(), call_key='first', invoke=fail)
    row = AIUsageReservation.objects.get()
    assert row.state == 'unknown' and row.actual_units is None
    assert all(account.reserved > 0 and account.spent == 0 for account in BudgetAccount.objects.all())
    job.attempts = 2
    with pytest.raises(ProviderFailure, match='AI_CALL_OUTCOME_UNKNOWN'):
        services.execute_budgeted_call(provider, session=session, job=job, context=ctx(), call_key='first', invoke=lambda: pytest.fail('repeated'))
    assert AIUsageReservation.objects.count() == 1
    # Late receipt after cancellation replaces the hold, never a free refund.
    services.settle(row, {'prompt_tokens': 1000, 'completion_tokens': 100}, 'late')
    row.refresh_from_db()
    assert row.state == 'settled' and row.actual_units == 1200
    services.mark_unknown(row)
    services.settle(row, {'prompt_tokens': 1000, 'completion_tokens': 100}, 'duplicate')
    assert BudgetAccount.objects.get(key=f'usd_micros:session:{session.id}').spent == 1200


def test_missing_usage_is_not_free(team):
    session, job, provider = fixture(team.org, provider='openai')
    services.execute_budgeted_call(provider, session=session, job=job, context=ctx(), call_key='first', invoke=lambda: {'valid': True})
    row = AIUsageReservation.objects.get()
    assert row.state == 'unknown'
    assert BudgetAccount.objects.get(key=f'usd_micros:session:{session.id}').reserved == row.max_units


def test_cancel_before_dispatch_releases_only_unstarted_reservation(team):
    session, job, provider = fixture(team.org)
    def cancelled():
        raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        services.execute_budgeted_call(provider, session=session, job=job, context=SimpleNamespace(check=cancelled), call_key='first', invoke=lambda: pytest.fail('sent'))
    assert AIUsageReservation.objects.get().state == 'released'
    assert all(account.reserved == 0 for account in BudgetAccount.objects.all())


def test_actual_over_bound_records_and_blocks_further_calls(team):
    session, job, provider = fixture(team.org, provider='openai')
    row = services.reserve(provider, job=job, session=session, call_key='first', frames_count=0)
    services.mark_dispatched(row)
    services.settle(row, {'prompt_tokens': 1000000, 'completion_tokens': 8192})
    row.refresh_from_db()
    assert row.actual_units > row.max_units
    assert BudgetAccount.objects.filter(blocked=False).count() == 0
    with pytest.raises(ProviderFailure, match='AI_BUDGET_EXCEEDED'):
        services.reserve(provider, job=job, session=session, call_key='second', frames_count=0)


def test_org_quota_spans_sessions_and_rolls_back_other_accounts(team, settings):
    settings.AI_BUDGET_ORG_DAY_CALLS = 1
    first, job, provider = fixture(team.org)
    services.execute_budgeted_call(provider, session=first, job=job, context=ctx(), call_key='first', invoke=lambda: {})
    second, job2, provider2 = fixture(team.org)
    with pytest.raises(ProviderFailure, match='AI_BUDGET_EXCEEDED'):
        services.reserve(provider2, job=job2, session=second, call_key='second')
    assert not BudgetAccount.objects.filter(key=f'calls:session:{second.id}').exists()
    assert AIUsageReservation.objects.count() == 1


def test_preflight_checks_full_baseline_without_consuming(team):
    session, job, provider = fixture(team.org, provider='openai', cash=1000000)
    with pytest.raises(ProviderFailure, match='AI_BUDGET_EXCEEDED'):
        services.preflight(session, 20, provider)
    assert BudgetAccount.objects.count() == 0
    session.analysis_budget['max_cost_microusd'] = 2000000
    assert services.preflight(session, 20, provider)['unit'] == 'usd_micros'


def test_body_bound_includes_schema_and_decoded_dimensions():
    with pytest.raises(ProviderFailure, match='AI_BUDGET_INPUT_UNBOUNDED'):
        enforce_budget_body({'messages': [], 'response_format': {'schema': 'x' * 32769}})
    buf = io.BytesIO()
    Image.new('RGB', (961, 10), 'red').save(buf, format='JPEG')
    body = {'messages': [{'content': [{'type': 'image_url', 'image_url': {'url': 'data'}}]}]}
    with pytest.raises(ProviderFailure, match='AI_BUDGET_INPUT_UNBOUNDED'):
        enforce_budget_body(body, [{'bytes': buf.getvalue()}])


@pytest.mark.django_db(transaction=True)
def test_parallel_reservation_cannot_overdraw(settings):
    org = Organization.objects.create(name='Concurrent budget')
    session, job, provider = fixture(org, calls=1)
    barrier = threading.Barrier(2)
    def run(index):
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            attempt = SimpleNamespace(id=uuid.uuid4(), attempts=1, organization_id=org.id)
            try:
                return services.reserve(provider, job=attempt, session=session, call_key=str(index)).id
            except ProviderFailure as error:
                return error.code
        finally:
            connections['default'].close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, [1, 2]))
    assert AIUsageReservation.objects.count() == 1, results
    assert BudgetAccount.objects.get(key=f'calls:session:{session.id}').reserved == 1


def test_local_ollama_has_call_quota_not_subscription_or_cash(team):
    session, job, provider = fixture(team.org, calls=1)
    provider.provider, provider.model, provider.endpoint = 'ollama', 'local-model', 'http://127.0.0.1:11434'
    services.execute_budgeted_call(provider, session=session, job=job, context=ctx(), call_key='local', invoke=lambda: {})
    assert services.budget_snapshot(session)['mode'] == 'local_calls'
    assert services.budget_snapshot(session)['spent_microusd'] is None
    assert AIUsageReservation.objects.get().actual_units == 1


def test_remote_ollama_cannot_claim_local_free_budget(team):
    session, job, provider = fixture(team.org)
    provider.provider, provider.model, provider.endpoint = 'ollama', 'remote-model', 'https://remote.example/v1'
    with pytest.raises(ProviderFailure, match='AI_PRICING_UNAVAILABLE'):
        services.execute_budgeted_call(provider, session=session, job=job, context=ctx(), call_key='remote', invoke=lambda: pytest.fail('sent'))
    assert AIUsageReservation.objects.count() == 0


def test_late_receipt_uses_reserved_pricing_snapshot(team, monkeypatch):
    session, job, provider = fixture(team.org, provider='openai')
    row = services.reserve(provider, job=job, session=session, call_key='pricing')
    services.mark_dispatched(row)
    monkeypatch.setitem(services.PRICES, 'gpt-5.4-mini', (300, 4, 900, 2))
    services.settle(row, {'prompt_tokens': 1000, 'completion_tokens': 100})
    row.refresh_from_db()
    assert row.actual_units == 1200
