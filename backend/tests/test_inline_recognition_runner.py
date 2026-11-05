"""Offline inline-v2 replay guards; the fake model never contacts a provider."""
import copy
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.db.models.signals import post_save

from ai_budget.models import AIUsageReservation
from media_pipeline.models import ProcessingJob
from visual_drafts import worker
from visual_drafts.models import VisualProposal, VisualSession
from .test_dense_recognition_runner import item as prepared_item
from .test_frame_grounding_v2 import wire as fact_wire
from .test_teaching_video_runner import source

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('check_inline_recognition', ROOT / 'scripts/check_inline_recognition.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def report():
    return {'version': 1, 'experiment': check.EXPERIMENT, 'max_calls': 2, 'run_id': str(uuid.uuid4()),
            'configuration': {'fixed': True}, 'cases': [],
            'budget': {'held_calls': 0, 'used_or_held_calls': 0, 'reservations': []}}


def job(case=check.CASES[0], state='done', config=None):
    return SimpleNamespace(id=uuid.uuid4(), state=state, payload={
        'benchmark_case': case, 'benchmark_experiment': check.EXPERIMENT,
        'benchmark_configuration_sha256': check.digest(config or {'fixed': True})})


def adapted_item(folder, original, index=0):
    prepared = prepared_item(folder, index)
    adapted = {**prepared, 'prepared_input_sha256': prepared['input_sha256'], 'clip_id': 'chopping',
               'arm': check.CASES[index].split(':')[1], 'working': original['working_before']}
    adapted['input_sha256'] = check.digest({key: value for key, value in adapted.items() if key != 'input_sha256'})
    return adapted


def test_existing_experiment_caps_and_pins_are_isolated():
    from .test_short_action_runner import check as old_short
    from .test_dense_recognition_runner import check as old_dense
    assert (old_short.EXPERIMENT, old_short.MAX_CALLS) == ('short-actions-v1', 4)
    assert (old_dense.EXPERIMENT, old_dense.MAX_CALLS) == ('recognition-dense-v1', 2)
    assert 'frame_grounding' not in old_short.analysis_options()
    assert 'frame_grounding' not in old_dense.core.analysis_options()
    from .test_grounded_recognition_runner import check as old_grounded
    assert (old_grounded.EXPERIMENT, old_grounded.MAX_CALLS) == ('recognition-grounded-v1', 2)
    assert old_grounded.analysis_options()['frame_grounding'] == 'v1'
    assert (check.EXPERIMENT, check.MAX_CALLS) == ('recognition-inline-v2', 2)
    assert check.core.analysis_options() == {
        'visual_protocol': '3', 'short_action_coverage': 'v1', 'frame_grounding': 'v2'}


@pytest.mark.parametrize('state', ['queued', 'running'])
def test_unresolved_job_blocks_next_window(state):
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_OUTCOME_UNRESOLVED'):
        check.guard(report(), [check.CASES[1]], {'fixed': True}, [job(state=state)])


def test_held_call_and_global_limit_survive_new_report():
    value = report()
    value['budget']['held_calls'] = 1
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_OUTCOME_UNRESOLVED'):
        check.guard(value, [check.CASES[1]], {'fixed': True}, [job()])
    value = report()
    value['budget']['used_or_held_calls'] = 2
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_CALL_LIMIT'):
        check.guard(value, [check.CASES[0]], {'fixed': True}, [])


def test_changed_configuration_and_duplicate_selection_block_before_dispatch():
    with pytest.raises(check.runner.BenchmarkFailure, match='INLINE_GLOBAL_CONFIGURATION_CHANGED'):
        check.guard(report(), [check.CASES[1]], {'fixed': True}, [job(config={'changed': True})])
    with pytest.raises(check.runner.BenchmarkFailure, match='INLINE_CASES_INVALID'):
        check.guard(report(), [check.CASES[0]] * 2, {'fixed': True}, [])


def test_default_cli_dry_run_never_invokes_model(tmp_path, monkeypatch, capsys):
    org = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(check.runner, 'initialize_django', lambda: None)
    monkeypatch.setattr(check, 'load_inputs', lambda: ({}, [], {}))
    monkeypatch.setattr(check, 'frozen_artifacts', lambda: ({'model_calls': 0}, {}))
    monkeypatch.setattr(check, 'prior_grounded_summary', lambda: {'model_calls_new': 0})
    monkeypatch.setattr(check, 'validate_source', lambda *_: ({'member': SimpleNamespace(organization=org)}, []))
    monkeypatch.setattr(check, 'configuration', lambda *_: {'fixed': True})
    monkeypatch.setattr(check, 'experiment_jobs', lambda: [])
    monkeypatch.setattr(check, 'refresh_budget', lambda value: value.update(budget=report()['budget']))
    monkeypatch.setattr(check.runner, 'quota_quote', lambda *_: None)
    monkeypatch.setattr(check.core, 'LOCK_PATH', tmp_path / '.lock')
    monkeypatch.setattr(check, 'run_case', lambda *_: pytest.fail('Dry run dispatched model'))
    output = tmp_path / 'report.json'
    monkeypatch.setattr(sys, 'argv', ['check_inline_recognition', '--output', str(output)])
    check.main()
    result = json.loads(output.read_text())
    assert result['status'] == 'dry_run_validated' and not result['cases']
    assert result['budget']['used_or_held_calls'] == 0
    assert result['paid_api_enabled'] is result['automatic_retries'] is result['refinements'] is False
    assert '"calls": 0' in capsys.readouterr().out


def test_execute_cli_stops_after_first_failure_without_repair_or_second_window(tmp_path, monkeypatch):
    org, dispatched = SimpleNamespace(id=uuid.uuid4()), []
    monkeypatch.setattr(check.runner, 'initialize_django', lambda: None)
    monkeypatch.setattr(check, 'load_inputs', lambda: ({}, [{'case_id': case} for case in check.CASES], {}))
    monkeypatch.setattr(check, 'frozen_artifacts', lambda: ({'model_calls': 0}, {}))
    monkeypatch.setattr(check, 'prior_grounded_summary', lambda: {'model_calls_new': 0})
    monkeypatch.setattr(check, 'validate_source', lambda *_: ({'member': SimpleNamespace(organization=org)}, []))
    monkeypatch.setattr(check, 'configuration', lambda *_: {'fixed': True})
    monkeypatch.setattr(check, 'experiment_jobs', lambda: [])
    monkeypatch.setattr(check, 'refresh_budget', lambda value: value.update(budget=report()['budget']))
    monkeypatch.setattr(check.runner, 'quota_quote', lambda *_: None)
    monkeypatch.setattr(check.core, 'LOCK_PATH', tmp_path / '.lock')

    def rejected(source, item, value, persist):
        dispatched.append(item['case_id'])
        value['cases'].append({'case_id': item['case_id'], 'status': 'failed'})
        value['budget']['used_or_held_calls'] = 1
        persist()
        check.fail('SHORT_STOPPED_NO_RETRY')

    monkeypatch.setattr(check, 'run_case', rejected)
    output = tmp_path / 'report.json'
    monkeypatch.setattr(sys, 'argv', ['check_inline_recognition', '--output', str(output), '--execute'])
    with pytest.raises(SystemExit) as stopped:
        check.main()
    assert stopped.value.code == 1 and dispatched == [check.CASES[0]]
    result = json.loads(output.read_text())
    assert result['status'] == 'halted' and result['error_code'] == 'SHORT_STOPPED_NO_RETRY'
    assert result['budget']['used_or_held_calls'] == 1


@pytest.mark.django_db
def test_invalid_model_output_consumes_one_call_and_never_retries(team, draft, tmp_path, monkeypatch):
    original = source(team, draft, clip='chopping')
    calls = []

    class InvalidModel:
        provider, model, endpoint = 'codex', 'gpt-6-luna', ''

        def __init__(self, organization):
            pass

        def prepare_request(self, **kwargs):
            assert self.frame_grounding == 'v2'

        def generate(self, *, language, mode, frames, context):
            calls.append(len(frames))
            value = fact_wire(frames, language)
            value['actions'][0]['evidence'][-1]['image_index'] = 99
            return value

    monkeypatch.setattr(worker, 'capabilities', lambda _: {'available': True})
    monkeypatch.setattr(worker, 'VisualProvider', InvalidModel)
    value = report()
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_STOPPED_NO_RETRY'):
        check.run_case(original, adapted_item(tmp_path, original), value, lambda: None)
    assert calls == [12]
    assert value['cases'][0]['validation']['status'] == 'rejected'
    assert value['cases'][0]['raw_model_response']['schema_version'] == '3-grounded-actions-v2'
    assert value['budget']['used_or_held_calls'] == value['budget']['settled_calls'] == 1
    assert value['budget']['held_calls'] == 0
    assert value['cases'][0]['working_after'] == original['working_before']
    assert ProcessingJob.objects.filter(payload__benchmark_experiment=check.EXPERIMENT).count() == 1
    assert AIUsageReservation.objects.count() == 1
    assert not VisualProposal.objects.filter(applied_at__isnull=False).exists()
    assert not VisualSession.objects.filter(analysis_windows__parent__isnull=False).exists()


@pytest.mark.django_db
def test_real_worker_two_settled_calls_pinned_at_insert_and_third_blocked(team, draft, tmp_path, monkeypatch):
    original = source(team, draft, clip='chopping')
    calls, inserted, checkpoints = [], [], []

    class LocalModel:
        provider, model, endpoint = 'codex', 'gpt-6-luna', ''

        def __init__(self, organization):
            pass

        def prepare_request(self, **kwargs):
            assert self.protocol == '3' and self.short_action_coverage == 'v1' and self.frame_grounding == 'v2'

        def generate(self, *, language, mode, frames, context):
            calls.append(len(frames))
            return fact_wire(frames, language)

    def capture_insert(sender, instance, created=False, **kwargs):
        if created and sender in {VisualSession, ProcessingJob}:
            pins = instance.analysis_budget if sender is VisualSession else instance.payload
            assert all(pins.get(key) == value for key, value in check.analysis_options().items())
            inserted.append(sender)

    monkeypatch.setattr(worker, 'capabilities', lambda _: {'available': True})
    monkeypatch.setattr(worker, 'VisualProvider', LocalModel)
    value = report()
    post_save.connect(capture_insert, weak=False)
    try:
        for index in range(2):
            check.run_case(original, adapted_item(tmp_path / str(index), original, index), value,
                           lambda: checkpoints.append(copy.deepcopy(value)))
            case = value['cases'][-1]
            assert case['validation']['status'] == 'passed'
            assert case['working_after'] == case['working_before'] == original['working_before']
            assert case['session_budget'] == {'max_calls': 1, 'max_cost_microusd': 0, **check.analysis_options()}
            assert case['raw_model_response']['schema_version'] == '3-grounded-actions-v2'
            assert case['proposal']['frame_grounding']['version'] == 'v2'
            assert len(case['proposal']['steps']) == 1  # Positive control: facts retain a grounded action.
    finally:
        post_save.disconnect(capture_insert)
    assert calls == [12, 12]
    assert inserted.count(VisualSession) == inserted.count(ProcessingJob) == 2
    assert AIUsageReservation.objects.count() == 2
    assert checkpoints[0]['cases'][0]['status'] == 'before_dispatch'
    assert not VisualProposal.objects.filter(applied_at__isnull=False).exists()
    for receipt in AIUsageReservation.objects.all():
        assert receipt.unit == 'calls' and receipt.max_units == receipt.actual_units == 1
        assert receipt.state == 'settled' and receipt.provider == 'codex' and receipt.model == 'gpt-6-luna'
    assert value['budget']['settled_calls'] == 2 and value['budget']['held_calls'] == 0
    assert all(row.payload['benchmark_configuration_sha256'] == check.digest(value['configuration'])
               for row in check.experiment_jobs())
    fresh = report()
    check.refresh_budget(fresh)
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_CASE_ALREADY_ATTEMPTED'):
        check.run_case(original, adapted_item(tmp_path / 'third', original), fresh, lambda: None)
    assert calls == [12, 12] and AIUsageReservation.objects.count() == 2
    assert not VisualSession.objects.filter(analysis_windows__parent__isnull=False).exists()
