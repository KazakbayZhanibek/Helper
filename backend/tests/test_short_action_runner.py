"""No cloud calls: fixed experiment slots and real ledger with a local model."""
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
spec = importlib.util.spec_from_file_location('check_short_actions', ROOT / 'scripts/check_short_actions.py')
check = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = check
spec.loader.exec_module(check)


def report():
    return {'version': 1, 'experiment': check.EXPERIMENT, 'max_calls': 4, 'run_id': str(uuid.uuid4()),
            'configuration': {'fixed': True}, 'cases': [],
            'budget': {'held_calls': 0, 'used_or_held_calls': 0, 'reservations': []}}


def job(case='kneading:events', state='done', experiment=check.EXPERIMENT):
    return SimpleNamespace(id=uuid.uuid4(), state=state,
                           payload={'benchmark_case': case, 'benchmark_experiment': experiment})


@pytest.mark.parametrize('value', ['', 'kneading', 'kneading:events,kneading:events', 'sewing:events', 'kneading:events,'])
def test_fixed_case_selector(value):
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_CASES_INVALID'):
        check.selected_cases(value)


def test_same_case_cannot_be_replayed_in_another_report():
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_CASE_ALREADY_ATTEMPTED'):
        check.guard(report(), ['kneading:events'], {'fixed': True}, [job()])
    check.guard(report(), ['kneading:short_events'], {'fixed': True}, [job()])


@pytest.mark.parametrize('state', ['running', 'queued'])
def test_prepared_unfinished_job_blocks_another_dispatch(state):
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_OUTCOME_UNRESOLVED'):
        check.guard(report(), ['kneading:short_events'], {'fixed': True}, [job(state=state)])


def test_global_four_attempts_cannot_be_reset_by_fresh_report():
    value = report()
    jobs = [job(case) for case in check.CASES]
    with pytest.raises(check.runner.BenchmarkFailure):
        check.guard(value, ['kneading:events'], {'fixed': True}, jobs)
    value['budget']['used_or_held_calls'] = 4
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_CALL_LIMIT'):
        check.guard(value, ['kneading:events'], {'fixed': True}, [])


@pytest.mark.parametrize('change', [{'experiment': 'new-reset'}, {'max_calls': 8}, {'configuration': {'changed': True}}])
def test_configuration_change_is_refused(change):
    value = report()
    value.update(change)
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_CONFIGURATION_CHANGED'):
        check.guard(value, ['kneading:events'], {'fixed': True}, [])


def test_held_receipt_blocks_even_new_pair():
    value = report()
    value['budget']['held_calls'] = 1
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_OUTCOME_UNRESOLVED'):
        check.guard(value, ['kneading:events'], {'fixed': True}, [])


def test_all_reports_and_previous_runners_share_the_lock(tmp_path, monkeypatch):
    shared = tmp_path / '.lock'
    monkeypatch.setattr(check, 'LOCK_PATH', shared)
    monkeypatch.setattr(check.previous, 'EXPERIMENT_LOCK', shared)
    with check.lock():
        with pytest.raises(check.runner.BenchmarkFailure, match='REGRESSION_ALREADY_RUNNING'):
            with check.previous.report_lock(tmp_path / 'old.json'):
                pytest.fail('Concurrent runner entered')


def bundle_item(tmp_path, *, case='kneading:events', start=0, end=24000):
    tmp_path.mkdir(parents=True, exist_ok=True)
    frames = []
    times = list(range(0, 24000, 2000))
    bounds = [start, *[(a+b)//2 for a,b in zip(times,times[1:])], end]
    raw = jpeg()
    for index, timestamp in enumerate(times, 1):
        path = tmp_path / f'{index}.jpg'
        path.write_bytes(raw)
        path.chmod(0o600)
        frames.append({'file': str(path), 'sequence': index, 'timestamp_ms': timestamp,
                       'width': 80, 'height': 60, 'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
                       'coverage_start_ms': bounds[index-1], 'coverage_end_ms': bounds[index]})
    item = {'case_id': case, 'clip_id': case.split(':')[0], 'arm': case.split(':')[1],
            'start_ms': start, 'end_ms': end, 'frames': frames, 'manifest': {}}
    item['input_sha256'] = check.digest(item)
    return item


def test_prepared_input_is_verified_without_mutation(tmp_path):
    item = bundle_item(tmp_path)
    before = copy.deepcopy(item)
    assert len(check.checked_input(item)) == 12
    assert item == before


@pytest.mark.parametrize('kind', ['bytes', 'mode', 'time', 'boundary', 'hash', 'arm'])
def test_modified_or_unbounded_prepared_frame_is_rejected(tmp_path, kind):
    item = bundle_item(tmp_path)
    path = Path(item['frames'][0]['file'])
    if kind == 'bytes':
        path.write_bytes(b'changed')
    elif kind == 'mode':
        path.chmod(0o644)
    elif kind == 'time':
        item['frames'][0]['timestamp_ms'] = 50000
    elif kind == 'boundary':
        item['frames'][0]['coverage_end_ms'] = 400
    elif kind == 'hash':
        item['input_sha256'] = '0'*64
    elif kind == 'arm':
        item['arm'] = 'short_events'
    if kind in {'time', 'boundary', 'arm'}:
        # Even a recomputed report hash cannot make malformed timing valid.
        item['input_sha256'] = check.digest({k:v for k,v in item.items() if k != 'input_sha256'})
    with pytest.raises(check.runner.BenchmarkFailure):
        check.checked_input(item)


def test_inside_sample_is_availability_not_recognition():
    result = check.sampling_coverage([{'id':'a','label':'move','importance':'primary','start_ms':250,'end_ms':800}], [0,600,800])
    assert result[0]['samples_inside_ms'] == [600]
    assert result[0]['nearest_before_ms'] == 0 and result[0]['nearest_after_ms'] == 800
    assert not any('recogn' in key for key in result[0])


def test_cli_default_is_preflight_without_jobs_or_model_calls(tmp_path, monkeypatch, capsys):
    source_path, input_path, output = tmp_path/'source.json', tmp_path/'inputs.json', tmp_path/'run.json'
    source_path.write_text('{}')
    fake_source = SimpleNamespace(id=uuid.uuid4(), instruction_id=uuid.uuid4(), asset=SimpleNamespace(sha256='a'*64))
    org = SimpleNamespace(id=uuid.uuid4())
    sources = {'old_session': fake_source, 'working_before': {'revision': 1},
               'member': SimpleNamespace(organization=org, organization_id=org.id)}
    inputs = []
    for case in check.CASES:
        inputs.append({'case_id': case, 'clip_id': case.split(':')[0], 'arm': case.split(':')[1],
                       'instruction_id': str(fake_source.instruction_id), 'source_session_id': str(fake_source.id),
                       'asset_sha256': fake_source.asset.sha256, 'working': sources['working_before']})
    input_path.write_text(json.dumps({'experiment': check.EXPERIMENT, 'model_calls': 0, 'inputs': inputs,
                                     'source_report_sha256': hashlib.sha256(source_path.read_bytes()).hexdigest()}))
    monkeypatch.setattr(check.runner, 'initialize_django', lambda: None)
    monkeypatch.setattr(check.previous, 'validate_source', lambda *_: sources)
    monkeypatch.setattr(check, 'configuration', lambda *_: {'fixed': True})
    monkeypatch.setattr(check, 'experiment_jobs', lambda: [])
    monkeypatch.setattr(check, 'refresh_budget', lambda value: value.update(budget=report()['budget']))
    monkeypatch.setattr(check.runner, 'quota_quote', lambda *_: None)
    monkeypatch.setattr(check, 'LOCK_PATH', tmp_path/'.lock')
    monkeypatch.setattr(check, 'create_case', lambda *_: pytest.fail('Preflight created a job'))
    monkeypatch.setattr(check, 'run_case', lambda *_: pytest.fail('Preflight dispatched'))
    monkeypatch.setattr(sys, 'argv', ['check_short_actions', '--inputs', str(input_path), '--source-report', str(source_path), '--output', str(output)])
    check.main()
    assert json.loads(output.read_text())['status'] == 'dry_run_validated'
    assert json.loads(output.read_text())['cases'] == []
    assert '"calls": 0' in capsys.readouterr().out


@pytest.mark.django_db
@pytest.mark.parametrize('failure', [False, True])
def test_normal_worker_receipt_and_checkpoint_with_no_auto_apply(team, draft, tmp_path, monkeypatch, failure):
    original = source(team, draft)
    item = bundle_item(tmp_path / 'inputs')
    item['working'] = original['working_before']
    item['input_sha256'] = check.digest({k:v for k,v in item.items() if k != 'input_sha256'})
    calls = install_local_model(monkeypatch, failure=failure)
    # This fixture verifies the runner/ledger, not the new inference contract.
    # The real coverage contract has independent tests in its owning module.
    validate = provider.validate
    seen = []
    def local_validate(value, frames, session, **kwargs):
        seen.append(kwargs)
        return validate(value, frames, session, expected_version='3')
    monkeypatch.setattr(worker, 'validate', local_validate)
    value, writes = report(), []
    if failure:
        with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_STOPPED_NO_RETRY'):
            check.run_case(original, item, value, lambda: writes.append(copy.deepcopy(value)))
    else:
        check.run_case(original, item, value, lambda: writes.append(copy.deepcopy(value)))
        assert value['cases'][0]['status'] == 'done'
        assert value['cases'][0]['identical_prepared_images_and_times']
        assert value['cases'][0]['working_before'] == value['cases'][0]['working_after']
        assert seen
    assert calls == [1]
    assert len(check.experiment_jobs()) == 1
    assert AIUsageReservation.objects.count() == 1
    assert writes[0]['cases'][0]['status'] == 'before_dispatch'
    assert not VisualProposal.objects.filter(applied_at__isnull=False).exists()
    receipt = AIUsageReservation.objects.get()
    assert receipt.unit == 'calls' and receipt.max_units == 1
    assert receipt.state == ('unknown' if failure else 'settled')
    with pytest.raises(check.runner.BenchmarkFailure, match='SHORT_CASE_ALREADY_ATTEMPTED'):
        check.guard(report(), [item['case_id']], {'fixed': True}, check.experiment_jobs())
