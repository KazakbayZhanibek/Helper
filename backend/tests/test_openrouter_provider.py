"""OpenRouter routing and spending boundaries. No network or real keys."""
import base64
import io
import json
import uuid
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from jsonschema import Draft202012Validator
from PIL import Image

from ai_budget import services as budget
from ai_budget.models import AIUsageReservation, BudgetAccount
from ai_drafts import providers
from visual_drafts import provider as vision

pytestmark = pytest.mark.django_db
FREE = 'openrouter/free'
PAID = providers.OPENROUTER_BUDGET_MODEL


@pytest.fixture(autouse=True)
def configured(team, settings):
    settings.STEP_DRAFT_BACKEND = 'openrouter'
    settings.STEP_DRAFT_ENDPOINT = providers.OPENROUTER_ENDPOINT
    settings.STEP_DRAFT_MODEL = FREE
    settings.OPENROUTER_API_KEY = 'router-fixture-not-a-key'
    settings.STEP_DRAFT_API_KEY = 'openai-fixture-not-a-key'
    settings.NVIDIA_API_KEY = 'nvidia-fixture-not-a-key'
    settings.OPENROUTER_TOTAL_BUDGET_MICROUSD = 1500000
    settings.EXTERNAL_AI_ENABLED = True
    team.org.external_ai_enabled = True


def run_fixture(team, model=FREE):
    session = SimpleNamespace(id=uuid.uuid4(), organization=team.org, organization_id=team.org.id,
                              analysis_budget={'max_calls': 30, 'max_cost_microusd': 100000})
    job = SimpleNamespace(id=uuid.uuid4(), organization_id=team.org.id, attempts=1)
    ai = SimpleNamespace(provider='openrouter', model=model, endpoint=providers.OPENROUTER_ENDPOINT)
    context = SimpleNamespace(check=lambda: None, call=lambda fn, timeout: fn())
    return session, job, ai, context


@pytest.mark.parametrize('disabled', ['key', 'global', 'organization'])
def test_requires_own_key_and_cloud_permissions(team, settings, disabled):
    if disabled == 'key':
        settings.OPENROUTER_API_KEY = ''
    elif disabled == 'global':
        settings.EXTERNAL_AI_ENABLED = False
    else:
        team.org.external_ai_enabled = False
    assert not providers.endpoint_configuration(team.org)['available']
    with pytest.raises(providers.ProviderFailure):
        vision.VisualProvider(team.org)


@pytest.mark.parametrize('endpoint', ['https://api.openai.com/v1', 'http://127.0.0.1:1234/v1',
                                   'https://openrouter.ai.evil/api/v1', 'https://openrouter.ai:8443/api/v1',
                                   'https://user@openrouter.ai/api/v1', 'https://openrouter.ai/api/v1?x=1'])
def test_noncanonical_endpoint_never_uses_key_or_reserves(team, settings, endpoint):
    settings.STEP_DRAFT_ENDPOINT = endpoint
    assert not providers.endpoint_configuration(team.org)['available']
    session, job, ai, _ = run_fixture(team)
    ai.endpoint = endpoint
    with pytest.raises(providers.ProviderFailure, match='AI_PRICING_UNAVAILABLE'):
        budget.reserve(ai, session=session, job=job, call_key='invalid')
    assert not AIUsageReservation.objects.exists()


@pytest.mark.parametrize('model', ['openrouter/auto', '@preset', 'google/other-paid-model',
                                'vendor/paid:free:online', 'vendor/paid:free\n', 'vendor/model:free@preset'])
def test_unpriced_models_cannot_bypass_routing_or_cash_adapter(team, settings, model):
    settings.STEP_DRAFT_MODEL = model
    assert not providers.endpoint_configuration(team.org)['available']
    with pytest.raises(providers.ProviderFailure, match='AI_PRICING_UNAVAILABLE'):
        budget.estimate_call('openrouter', model)


@pytest.mark.parametrize('model', [FREE, 'vendor/vision:free', PAID])
@pytest.mark.parametrize('kind', ['speech', 'vision'])
def test_real_provider_payload_preserves_frames_schema_and_price_cap(team, settings, monkeypatch, model, kind):
    settings.STEP_DRAFT_MODEL = model
    captured, receipts = [], []
    candidate = {'schema_version': '2' if kind == 'vision' else '1', 'language': 'ru', 'steps': [], 'warnings': []}
    def response(url, body, **kwargs):
        captured.append((url, body, kwargs))
        return {'id': 'receipt', 'model': model, 'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'cost': 0.000018},
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(candidate)}}]}
    monkeypatch.setattr(vision if kind == 'vision' else providers, 'json_request', response)
    if kind == 'vision':
        buf = io.BytesIO()
        Image.new('RGB', (100, 80), 'blue').save(buf, format='JPEG')
        frames = [{'id': str(uuid.uuid4()), 'sequence': i, 'timestamp_ms': i * 1000, 'bytes': buf.getvalue()} for i in [1, 2]]
        ai = vision.VisualProvider(team.org)
        ai._usage_recorder = lambda *args: receipts.append(args)
        assert ai.generate(language='ru', mode='live', frames=frames) == candidate
    else:
        ai = providers.StepDraftProvider(team.org)
        ai._usage_recorder = lambda *args: receipts.append(args)
        assert ai.generate({'segments': []}) == candidate
    assert len(captured) == 1
    url, body, options = captured[0]
    assert url == providers.OPENROUTER_ENDPOINT + '/chat/completions'
    assert options['api_key'] == 'router-fixture-not-a-key'
    assert body['model'] == model
    assert body['max_tokens'] == 8192 and body['stream'] is False
    assert body['response_format']['json_schema']['strict'] is True
    assert not {'models', 'tools', 'plugins', 'store'} & body.keys()
    routing = body['provider']
    assert routing['require_parameters'] is True and routing['allow_fallbacks'] is False
    assert routing['data_collection'] == 'deny'
    assert routing['max_price']['prompt'] == (0.10 if model == PAID else 0)
    assert routing['max_price']['completion'] == (0.40 if model == PAID else 0)
    if model == PAID:
        assert body['reasoning'] == {'enabled': False}
    assert receipts[0][0]['resolved_model'] == model
    if kind == 'vision':
        images = [v for v in body['messages'][1]['content'] if v['type'] == 'image_url']
        assert len(images) == len(frames)
        assert [base64.b64decode(v['image_url']['url'].split(',', 1)[1]) for v in images] == [f['bytes'] for f in frames]


def test_free_daily_cap_is_global_and_preflight_is_read_only(team, settings):
    settings.OPENROUTER_FREE_DAY_CALLS = 1
    session, job, ai, context = run_fixture(team)
    budget.preflight(session, 1, ai)
    assert not BudgetAccount.objects.exists()
    def invoke():
        ai._usage_recorder({'resolved_model': 'vendor/vision:free'}, 'one')
        return {}
    budget.execute_budgeted_call(ai, session=session, job=job, context=context, call_key='one', invoke=invoke)
    assert AIUsageReservation.objects.get().usage['resolved_model'] == 'vendor/vision:free'
    assert budget.budget_snapshot(session)['mode'] == 'free_calls'
    other, job2, ai2, _ = run_fixture(team)
    with pytest.raises(providers.ProviderFailure, match='AI_BUDGET_EXCEEDED'):
        budget.reserve(ai2, session=other, job=job2, call_key='two')
    assert not BudgetAccount.objects.filter(key=f'calls:session:{other.id}').exists()


def test_paid_lifetime_cap_survives_sessions_and_counts_unknown_calls(team, settings):
    session, job, ai, _ = run_fixture(team, PAID)
    quote = budget.estimate_call('openrouter', PAID, 12)
    assert quote['max_units'] == 12674
    settings.OPENROUTER_TOTAL_BUDGET_MICROUSD = quote['max_units']
    row = budget.reserve(ai, session=session, job=job, call_key='one')
    budget.mark_dispatched(row)
    budget.mark_unknown(row)
    other, job2, ai2, _ = run_fixture(team, PAID)
    with pytest.raises(providers.ProviderFailure, match='AI_BUDGET_EXCEEDED'):
        budget.preflight(other, 1, ai2)
    with pytest.raises(providers.ProviderFailure, match='AI_BUDGET_EXCEEDED'):
        budget.reserve(ai2, session=other, job=job2, call_key='two')
    budget.settle(row, {'cost': '0.0000011', 'resolved_model': PAID}, 'late')
    row.refresh_from_db()
    assert row.actual_units == 2 and row.state == 'settled'
    assert row.usage['cost_microusd'] == 2
    assert budget.budget_snapshot(session)['mode'] == 'usd'
    assert BudgetAccount.objects.get(key='usd_micros:openrouter-total:v1').spent == 2


@pytest.mark.parametrize('cost', [None, True, -1, 'NaN', 'Infinity', {}, 'wrong'])
def test_missing_or_invalid_billed_cost_keeps_full_reservation(team, cost):
    session, job, ai, _ = run_fixture(team, PAID)
    row = budget.reserve(ai, session=session, job=job, call_key='one')
    budget.mark_dispatched(row)
    budget.settle(row, {'prompt_tokens': 10, 'completion_tokens': 2, 'cost': cost})
    row.refresh_from_db()
    assert row.state == 'unknown' and row.actual_units is None
    assert BudgetAccount.objects.get(key='usd_micros:openrouter-total:v1').reserved == row.max_units


def test_paid_cost_overrun_blocks_all_accounts(team):
    session, job, ai, _ = run_fixture(team, PAID)
    row = budget.reserve(ai, session=session, job=job, call_key='one')
    budget.mark_dispatched(row)
    budget.settle(row, {'cost': 0.1})
    assert BudgetAccount.objects.filter(blocked=False).count() == 0


def test_paid_disabled_without_explicit_lifetime_limit(team, settings):
    settings.OPENROUTER_TOTAL_BUDGET_MICROUSD = 0
    session, job, ai, context = run_fixture(team, PAID)
    with pytest.raises(providers.ProviderFailure, match='AI_BUDGET_EXCEEDED'):
        budget.execute_budgeted_call(ai, session=session, job=job, context=context, call_key='one', invoke=lambda: pytest.fail('sent'))


def test_http_429_does_not_retry_or_refund_a_dispatched_call(team, monkeypatch):
    attempts = []
    class Opener:
        def open(self, request, timeout):
            attempts.append(request)
            raise HTTPError(request.full_url, 429, 'quota', {}, None)
    monkeypatch.setattr(providers, 'build_opener', lambda *args: Opener())
    session, job, ai, context = run_fixture(team)
    with pytest.raises(providers.ProviderFailure, match='AI_RATE_LIMITED'):
        budget.execute_budgeted_call(ai, session=session, job=job, context=context, call_key='one',
                                     invoke=lambda: providers.json_request(ai.endpoint + '/chat/completions', {}))
    assert len(attempts) == 1
    assert AIUsageReservation.objects.get().state == 'unknown'
    with pytest.raises(providers.ProviderFailure, match='AI_CALL_OUTCOME_UNKNOWN'):
        budget.reserve(ai, session=session, job=job, call_key='one')


def test_gemini_unbounded_aspect_ratio_is_rejected_before_transport():
    buf = io.BytesIO()
    Image.new('RGB', (960, 1)).save(buf, format='JPEG')
    body = {'model': PAID, 'messages': [{'content': [{'type': 'image_url', 'image_url': {'url': 'data'}}]}]}
    with pytest.raises(providers.ProviderFailure, match='AI_BUDGET_INPUT_UNBOUNDED'):
        providers.enforce_budget_body(body, [{'bytes': buf.getvalue()}])


def test_gemini_wire_schema_keeps_shape_and_original_local_validation():
    original = {'type': 'object', 'additionalProperties': False, 'required': ['steps', 'format'], 'properties': {
        'format': {'const': 'v1'},
        'steps': {'type': 'array', 'maxItems': 2, 'items': {'type': 'integer', 'minimum': 0, 'maximum': 600000}},
    }}
    before = json.dumps(original, sort_keys=True)
    wire = providers.schema_for_provider('openrouter', PAID, original)
    assert json.dumps(original, sort_keys=True) == before
    assert wire['properties']['format'] == {'enum': ['v1']}
    assert Draft202012Validator(wire).is_valid({'format': 'v1', 'steps': [-1, 2, 3]})
    assert not Draft202012Validator(original).is_valid({'format': 'v1', 'steps': [-1, 2, 3]})
    for value in [{'format': 'wrong', 'steps': []}, {'format': 'v1', 'steps': ['wrong']},
                  {'format': 'v1', 'steps': [], 'extra': 1}, {'steps': []}]:
        assert not Draft202012Validator(wire).is_valid(value)
    assert providers.schema_for_provider('openai', PAID, original) == original
    assert providers.schema_for_provider('openrouter', FREE, original) == original
