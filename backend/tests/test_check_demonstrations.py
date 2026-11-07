"""Bounded comparison script never dispatches a paid fallback for free tests."""
import importlib.util
from pathlib import Path

import pytest

from ai_drafts.providers import chat_completion_options


path = Path(__file__).resolve().parents[2] / 'scripts' / 'check_demonstrations.py'
spec = importlib.util.spec_from_file_location('check_demonstrations', path)
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)


@pytest.mark.parametrize('model', ['openrouter/free', 'google/gemini-2.5-flash-lite',
                                  'vendor/model:free:online', 'vendor/model:free\n', '@preset'])
def test_explicit_model_must_be_a_specific_free_route(model):
    with pytest.raises(ValueError):
        script.select_model(model)


def test_free_route_has_no_paid_fallback_and_zero_budget():
    model, cap = script.select_model('vendor/model:free')
    assert cap == 0
    route = chat_completion_options('openrouter', model)['provider']
    assert route['allow_fallbacks'] is False
    assert route['max_price'] == {'prompt': 0, 'completion': 0, 'request': 0, 'image': 0}


def test_paid_baseline_is_unchanged_without_model_argument():
    assert script.select_model(None) == ('google/gemini-2.5-flash-lite', 13000)
    assert script.select_model('qwen/qwen3.5-flash-02-23') == ('qwen/qwen3.5-flash-02-23', 13000)


def test_receipts_must_match_selected_model_and_budget():
    paid = {'provider': 'openrouter', 'model': 'google/gemini-2.5-flash-lite',
            'unit': 'usd_micros', 'max_units': 12674, 'usage': {}}
    script.validate_receipts([paid], paid['model'], 13000)
    free = {'provider': 'openrouter', 'model': 'vendor/model:free',
            'unit': 'calls', 'max_units': 1, 'usage': {'cost_microusd': 0, 'resolved_model': 'vendor/model:free'}}
    script.validate_receipts([free], free['model'], 0)
    with pytest.raises(RuntimeError):
        script.validate_receipts([], free['model'], 0)
    with pytest.raises(RuntimeError):
        script.validate_receipts([free], 'other/model:free', 0)
    with pytest.raises(RuntimeError):
        script.validate_receipts([{**free, 'usage': {'cost_microusd': 1}}], free['model'], 0)
    with pytest.raises(RuntimeError):
        script.validate_receipts([{**free, 'usage': {'resolved_model': 'vendor/model:free'}}], free['model'], 0)
    with pytest.raises(RuntimeError):
        script.validate_receipts([{**free, 'usage': {'cost_microusd': 0, 'resolved_model': 'other/model:free'}}], free['model'], 0)
    with pytest.raises(RuntimeError):
        script.validate_receipts([{**paid, 'max_units': 13001}], paid['model'], 13000)
