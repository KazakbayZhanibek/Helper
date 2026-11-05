"""Only local fake inference; exercise the real worker ledger and fixed slots."""
import copy
import hashlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_budget.models import AIUsageReservation
from visual_drafts import worker, provider
from visual_drafts.models import VisualProposal
from .test_teaching_video_runner import source, install_local_model
from .test_visual_drafts import jpeg

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('check_dense_recognition', ROOT / 'scripts/check_dense_recognition.py')
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


def item(folder, index=0):
    folder.mkdir(parents=True, exist_ok=True)
    start, end = index * 5880, (index + 1) * 5880
    times = [start + i * 490 for i in range(12)]
    bounds = [start, *[(a + b) // 2 for a, b in zip(times, times[1:])], end]
    raw, frames = jpeg(), []
    for seq, timestamp in enumerate(times, 1):
        path = folder / f'{seq}.jpg'
        path.write_bytes(raw)
        path.chmod(0o600)
        frames.append({'file': str(path), 'sequence': seq, 'timestamp_ms': timestamp, 'width': 80, 'height': 60,
                       'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                       'coverage_start_ms': bounds[seq - 1], 'coverage_end_ms': bounds[seq]})
    value = {'case_id': check.CASES[index], 'start_ms': start, 'end_ms': end,
             'strategy': 'short_events', 'frames': frames, 'manifest': {}}
    value['input_sha256'] = check.digest(value)
    return value


def immutable_fixture(tmp_path, monkeypatch):
    inputs = [item(tmp_path / str(i), i) for i in range(2)]
    parent = {'case_id': 'chopping:short_events', 'input_sha256': 'a' * 64,
              'frames': inputs[0]['frames'][:6] + inputs[1]['frames'][:6]}
    old = {'case_id': parent['case_id'], 'status': 'done', 'input_sha256': parent['input_sha256'],
           'sent_frames': parent['frames'], 'working_before': {'revision': 1},
           'working_after': {'revision': 1}, 'instruction_id': 'instruction'}
    for key, data in [('SOURCE_REPORT', {}), ('BASELINE_INPUTS', {'inputs': [parent]}),
                      ('BASELINE', {'cases': [old]})]:
        path = tmp_path / (key + '.json')
        path.write_text(json.dumps(data))
        monkeypatch.setattr(check, key, path)
    bundle = {'version': 2, 'model_calls': 0, 'scope': 'offline_anchored_dense_frame_preparation_only',
              'source_report_sha256': check.file_sha(check.SOURCE_REPORT),
              'baseline_input_bundle_sha256': check.file_sha(check.BASELINE_INPUTS),
              'working_before': old['working_before'], 'working_after': old['working_after'],
              'instruction_id': old['instruction_id'], 'asset_sha256': 'b' * 64,
              'preparation_code_sha256': {}, 'inputs': inputs}
    path = tmp_path / 'inputs.json'
    path.write_text(json.dumps(bundle))
    monkeypatch.setattr(check, 'INPUTS', path)
    monkeypatch.setattr(check, 'INPUT_SHA', check.file_sha(path))
    monkeypatch.setattr(check, 'BASELINE_SHA', check.file_sha(check.BASELINE))
    return bundle


@pytest.mark.parametrize('value', ['', 'chopping', 'chopping:anchored-1,chopping:anchored-1', 'chopping:anchored-3'])
def test_exact_two_case_selector(value):
    with pytest.raises(check.runner.BenchmarkFailure):
        check.selected_cases(value)


def test_previous_experiment_caps_are_not_changed():
    from .test_short_action_runner import check as original
    assert original.EXPERIMENT == 'short-actions-v1' and original.MAX_CALLS == 4
    assert check.EXPERIMENT == 'recognition-dense-v1' and check.MAX_CALLS == 2


def test_duplicate_across_reports_and_duplicate_selection_blocked():
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_CASE_ALREADY_ATTEMPTED'):
        check.guard(report(), [check.CASES[0]], {'fixed': True}, [job()])
    with pytest.raises(check.runner.BenchmarkFailure, match='DENSE_CASES_INVALID'):
        check.guard(report(), [check.CASES[0]] * 2, {'fixed': True}, [])
    check.guard(report(), [check.CASES[1]], {'fixed': True}, [job()])


@pytest.mark.parametrize('state', ['queued', 'running'])
def test_orphan_job_without_checkpoint_blocks_other_case(state):
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_OUTCOME_UNRESOLVED'):
        check.guard(report(), [check.CASES[1]], {'fixed': True}, [job(state=state)])


def test_global_two_call_cap_survives_fresh_report():
    value = report()
    value['budget']['used_or_held_calls'] = 2
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_CALL_LIMIT'):
        check.guard(value, [check.CASES[0]], {'fixed': True}, [])
    with pytest.raises(check.runner.BenchmarkFailure):
        check.guard(report(), [check.CASES[0]], {'fixed': True}, [job(case) for case in check.CASES])


def test_configuration_is_pinned_globally_not_just_per_report():
    with pytest.raises(check.runner.BenchmarkFailure, match='DENSE_GLOBAL_CONFIGURATION_CHANGED'):
        check.guard(report(), [check.CASES[1]], {'fixed': True}, [job(config={'different_prompt': True})])
    orphan = job()
    del orphan.payload['benchmark_configuration_sha256']
    with pytest.raises(check.runner.BenchmarkFailure, match='DENSE_GLOBAL_CONFIGURATION_CHANGED'):
        check.guard(report(), [check.CASES[1]], {'fixed': True}, [orphan])


def test_held_call_blocks_second_half():
    value = report()
    value['budget']['held_calls'] = 1
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_OUTCOME_UNRESOLVED'):
        check.guard(value, [check.CASES[1]], {'fixed': True}, [job()])


def test_input_adapter_retains_original_hash_and_bytes(tmp_path, monkeypatch):
    bundle = immutable_fixture(tmp_path, monkeypatch)
    _, inputs, _ = check.load_inputs()
    for prepared, adapted in zip(bundle['inputs'], inputs):
        assert adapted['prepared_input_sha256'] == prepared['input_sha256']
        assert adapted['frames'] == prepared['frames']
        assert len(check.core.checked_input(adapted)) == 12


@pytest.mark.parametrize('kind', ['pinned_bundle', 'baseline', 'source', 'jpeg', 'window', 'case', 'frame_hash'])
def test_tamper_is_rejected_before_dispatch(tmp_path, monkeypatch, kind):
    bundle = immutable_fixture(tmp_path, monkeypatch)
    if kind in {'pinned_bundle', 'baseline', 'source'}:
        path = {'pinned_bundle': check.INPUTS, 'baseline': check.BASELINE, 'source': check.SOURCE_REPORT}[kind]
        path.write_text(path.read_text() + ' ')
    elif kind == 'jpeg':
        Path(bundle['inputs'][0]['frames'][0]['file']).write_bytes(b'tampered')
    else:
        if kind == 'window':
            bundle['inputs'][1]['start_ms'] = 5800
        elif kind == 'case':
            bundle['inputs'][1]['case_id'] = check.CASES[0]
        else:
            bundle['inputs'][0]['frames'][0]['sha256'] = '0' * 64
        check.INPUTS.write_text(json.dumps(bundle))
        monkeypatch.setattr(check, 'INPUT_SHA', check.file_sha(check.INPUTS))
    with pytest.raises(check.runner.BenchmarkFailure):
        check.load_inputs()


def test_cli_default_only_preflights(tmp_path, monkeypatch, capsys):
    immutable_fixture(tmp_path, monkeypatch)
    org = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(check.runner, 'initialize_django', lambda: None)
    monkeypatch.setattr(check, 'validate_source', lambda *_: ({'member': SimpleNamespace(organization=org)}, {'model_calls': 0}))
    monkeypatch.setattr(check, 'configuration', lambda *_: {'fixed': True})
    monkeypatch.setattr(check, 'experiment_jobs', lambda: [])
    monkeypatch.setattr(check, 'refresh_budget', lambda r: r.update(budget=report()['budget']))
    monkeypatch.setattr(check.runner, 'quota_quote', lambda *_: None)
    monkeypatch.setattr(check.core, 'LOCK_PATH', tmp_path / '.lock')
    monkeypatch.setattr(check, 'run_case', lambda *_: pytest.fail('Dry run dispatched model'))
    output = tmp_path / 'report.json'
    monkeypatch.setattr(sys, 'argv', ['check_dense_recognition', '--output', str(output)])
    check.main()
    result = json.loads(output.read_text())
    assert result['status'] == 'dry_run_validated' and not result['cases']
    assert result['baseline']['model_calls'] == 0
    assert '"calls": 0' in capsys.readouterr().out


@pytest.mark.django_db
@pytest.mark.parametrize('failure', [False, True])
def test_real_ledger_with_local_model_preserves_working_and_caps_two(team, draft, tmp_path, monkeypatch, failure):
    original = source(team, draft, clip='chopping')
    calls = install_local_model(monkeypatch, failure=failure)
    validate = provider.validate
    monkeypatch.setattr(worker, 'validate', lambda value, frames, session, **_: validate(value, frames, session, expected_version='3'))
    value, checkpoints = report(), []
    for index in range(2):
        prepared = item(tmp_path / str(index), index)
        adapted = {**prepared, 'prepared_input_sha256': prepared['input_sha256'], 'clip_id': 'chopping',
                   'arm': check.CASES[index].split(':')[1], 'working': original['working_before']}
        adapted['input_sha256'] = check.digest({k: v for k, v in adapted.items() if k != 'input_sha256'})
        if failure:
            with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_STOPPED_NO_RETRY'):
                check.run_case(original, adapted, value, lambda: checkpoints.append(copy.deepcopy(value)))
            fresh = report()
            check.refresh_budget(fresh)
            with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_OUTCOME_UNRESOLVED'):
                check.guard(fresh, [check.CASES[1]], {'fixed': True}, check.experiment_jobs())
            break
        check.run_case(original, adapted, value, lambda: checkpoints.append(copy.deepcopy(value)))
        assert value['cases'][-1]['working_after'] == original['working_before']
    assert len(calls) == (1 if failure else 2) == AIUsageReservation.objects.count()
    assert checkpoints[0]['cases'][0]['status'] == 'before_dispatch'
    assert not VisualProposal.objects.filter(applied_at__isnull=False).exists()
    for receipt in AIUsageReservation.objects.all():
        assert receipt.unit == 'calls' and receipt.max_units == 1
        assert receipt.state == ('unknown' if failure else 'settled')
        if not failure:
            assert receipt.actual_units == 1
    assert all(j.payload['benchmark_configuration_sha256'] == check.digest(value['configuration']) for j in check.experiment_jobs())
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_CASE_ALREADY_ATTEMPTED'):
        check.guard(report(), [check.CASES[0]], {'fixed': True}, check.experiment_jobs())


@pytest.mark.django_db
def test_changed_working_copy_stops_before_creating_job(team, draft, tmp_path):
    original = source(team, draft, clip='chopping')
    prepared = item(tmp_path)
    adapted = {**prepared, 'clip_id': 'chopping', 'arm': 'anchored-1', 'working': {'revision': -1}}
    adapted['input_sha256'] = check.digest({k: v for k, v in adapted.items() if k != 'input_sha256'})
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_WORKING_CHANGED'):
        check.run_case(original, adapted, report(), lambda: None)
    assert not check.experiment_jobs() and not AIUsageReservation.objects.exists()
