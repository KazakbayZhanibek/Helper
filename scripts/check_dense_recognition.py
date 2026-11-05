"""Two-call anchored chopping check; default is a no-call preflight.

The archived short-events result is the baseline (never called again). Only
--execute may dispatch, once per fixed half, across reports and restarts.
Results remain separate unapplied proposals; the report groups both halves.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
# Reuse the audited exact-byte replay/ledger worker, without changing the old
# experiment's module globals or granting its exhausted slots a new allowance.
_spec = importlib.util.spec_from_file_location('_dense_replay_core', ROOT / 'scripts/check_short_actions.py')
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
runner, previous = core.runner, core.previous
# Case names identify the two halves, not extractor strategies. Keep the
# isolated helper's dispatch override on the real prepared-frame strategy.
core.runner = SimpleNamespace(**vars(runner))
core.runner.execute_case = lambda job, _arm: runner.execute_case(job, 'short_events')
EXPERIMENT, MAX_CALLS = 'recognition-dense-v1', 2
CASES = ('chopping:anchored-1', 'chopping:anchored-2')
INPUTS = ROOT / 'artifacts/test-results/recognition-dense-inputs-v2.json'
BASELINE = ROOT / 'artifacts/test-results/short-actions-run-v1.json'
BASELINE_INPUTS = ROOT / 'artifacts/test-results/short-actions-inputs-v2.json'
SOURCE_REPORT = ROOT / 'artifacts/test-results/real-video-ab-v1.json'
CRITERIA = ROOT / 'artifacts/test-results/dense-recognition-criteria-v1.json'
INPUT_SHA = 'c6f5c7eccb3ab6f2555bcc61d32fab8b85f1900ea47679de48bfc855634ac286'
BASELINE_SHA = '9dd566cdd7fdc1693eb893b974aa716ea38aea99d224d5bd9485c55880c0863f'
CRITERIA_SHA = '5fd641cc402e72adcd2b033bd01f2d0302315cf3e03babe215b527db8605c025'
core.EXPERIMENT, core.MAX_CALLS, core.CASES = EXPERIMENT, MAX_CALLS, CASES
core.CODE_FILES = (*core.CODE_FILES, 'scripts/check_dense_recognition.py')
digest, fail, selected_cases = core.digest, core.fail, core.selected_cases
experiment_jobs, refresh_budget = core.experiment_jobs, core.refresh_budget


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_inputs():
    """Validate immutable preparation artifacts before touching any job."""
    if file_sha(INPUTS) != INPUT_SHA or file_sha(BASELINE) != BASELINE_SHA:
        fail('DENSE_PINNED_ARTIFACT_CHANGED')
    bundle, baseline = json.loads(INPUTS.read_text()), json.loads(BASELINE.read_text())
    if (bundle.get('version') != 2 or bundle.get('model_calls') != 0
            or bundle.get('scope') != 'offline_anchored_dense_frame_preparation_only'
            or bundle['source_report_sha256'] != file_sha(SOURCE_REPORT)
            or bundle['baseline_input_bundle_sha256'] != file_sha(BASELINE_INPUTS)
            or bundle['working_before'] != bundle['working_after']
            or tuple(item['case_id'] for item in bundle['inputs']) != CASES
            or [(i['start_ms'], i['end_ms']) for i in bundle['inputs']] != [(0, 5880), (5880, 11760)]):
        fail('DENSE_INPUT_BUNDLE_INVALID')
    for name, expected in bundle['preparation_code_sha256'].items():
        if file_sha(ROOT / name) != expected:
            fail('DENSE_PREPARATION_CODE_CHANGED')
    old = [case for case in baseline['cases'] if case['case_id'] == 'chopping:short_events']
    if len(old) != 1 or old[0]['status'] != 'done':
        fail('DENSE_BASELINE_INVALID')
    old = old[0]
    parent = next(i for i in json.loads(BASELINE_INPUTS.read_text())['inputs'] if i['case_id'] == old['case_id'])
    signature = previous.frame_signature
    if (old['input_sha256'] != parent['input_sha256']
            or [signature(f) for f in old['sent_frames']] != [signature(f) for f in parent['frames']]
            or old['working_before'] != bundle['working_before'] or old['working_after'] != bundle['working_before']
            or old['instruction_id'] != bundle['instruction_id']):
        fail('DENSE_BASELINE_CHANGED')
    inputs = []
    for original in bundle['inputs']:
        if original['input_sha256'] != digest({k: v for k, v in original.items() if k != 'input_sha256'}):
            fail('DENSE_INPUT_CHANGED')
        item = {**copy.deepcopy(original), 'clip_id': 'chopping', 'arm': original['case_id'].split(':')[1],
                'prepared_input_sha256': original['input_sha256'], 'working': bundle['working_before'],
                'asset_sha256': bundle['asset_sha256']}
        item['input_sha256'] = digest({k: v for k, v in item.items() if k != 'input_sha256'})
        core.checked_input(item)
        inputs.append(item)
    all_frames = {signature(f) for item in inputs for f in item['frames']}
    if not {signature(f) for f in old['sent_frames']}.issubset(all_frames):
        fail('DENSE_BASELINE_FRAMES_NOT_PRESERVED')
    return bundle, inputs, old


def validate_source(bundle, baseline):
    from visual_drafts.models import VisualProposal
    from visual_drafts.services import proposal_data
    source = previous.validate_source(json.loads(SOURCE_REPORT.read_text()), 'chopping')
    old = source['old_session']
    if (str(old.id) != bundle['source_session_id'] or source['old_case']['job_id'] != bundle['source_job_id']
            or str(old.instruction_id) != bundle['instruction_id'] or str(old.asset_id) != bundle['asset_id']
            or old.asset.sha256 != bundle['asset_sha256'] or source['working_before'] != bundle['working_before']):
        fail('DENSE_SOURCE_CHANGED')
    proposal = VisualProposal.objects.select_related('session__instruction', 'session__asset').filter(
        pk=baseline['proposal_id'], source_job_id=baseline['job_id'], session_id=baseline['session_id']).first()
    if (not proposal or proposal.applied_at or proposal.session.instruction_id != old.instruction_id
            or proposal.session.asset_id != old.asset_id or proposal.session.state != 'completed'
            or proposal.session.frames_purged_at or proposal.content != baseline['proposal']):
        fail('DENSE_BASELINE_SOURCE_CHANGED')
    frames = list(proposal.session.frames.order_by('timestamp_ms').values(*previous.FRAME_FIELDS))
    if [previous.frame_signature(f) for f in frames] != [previous.frame_signature(f) for f in baseline['sent_frames']]:
        fail('DENSE_BASELINE_SOURCE_CHANGED')
    # Local current projection only: no model invocation and no proposal save.
    baseline_view = {'case_id': baseline['case_id'], 'job_id': baseline['job_id'],
                     'proposal_id': baseline['proposal_id'], 'model_calls': 0,
                     'wall_ms_original': baseline['wall_ms'], 'raw_model_response': baseline['raw_model_response'],
                     'original_proposal': baseline['proposal'], 'current_projection': proposal_data(proposal)}
    return source, baseline_view


def configuration(organization, inputs):
    # Re-read every byte before dispatch, including between the two calls.
    _, verified, _ = load_inputs()
    if verified != inputs or file_sha(CRITERIA) != CRITERIA_SHA:
        fail('DENSE_INPUT_CHANGED')
    config = core.configuration(organization, inputs)
    config.update(input_bundle_sha256=file_sha(INPUTS), baseline_report_sha256=file_sha(BASELINE),
                  criteria_sha256=file_sha(CRITERIA), reasoning_effort='medium',
                  frame_strategy='short_events', max_calls=2, max_cost_microusd=0)
    return config


def guard(report, selected, config, jobs):
    if not selected or len(set(selected)) != len(selected):
        fail('DENSE_CASES_INVALID')
    if any(job.payload.get('benchmark_configuration_sha256') != digest(config) for job in jobs):
        fail('DENSE_GLOBAL_CONFIGURATION_CHANGED')
    core.guard(report, selected, config, jobs)


_create_case = core.create_case


def create_case(source, item, run_id):
    from django.db import transaction
    if not source.get('candidate_sha256'):
        fail('DENSE_CONFIGURATION_REQUIRED')
    with transaction.atomic():
        job, session = _create_case(source, item, run_id)
        job.payload['benchmark_configuration_sha256'] = source['candidate_sha256']
        job.save(update_fields=['payload'])
        return job, session


core.create_case = create_case


def run_case(source, item, report, persist):
    refresh_budget(report)
    guard(report, [item['case_id']], report['configuration'], experiment_jobs())
    source = {**source, 'candidate_sha256': digest(report['configuration'])}
    core.run_case(source, item, report, persist)
    case = report['cases'][-1]
    receipts = [r for r in report['budget']['reservations'] if str(r['job_id']) == case['job_id']]
    if (len(receipts) != 1 or receipts[0]['actual_units'] != 1
            or receipts[0]['provider'] != 'codex' or receipts[0]['model'] != 'gpt-6-luna'):
        fail('DENSE_RECEIPT_INVALID')
    case['window_start_ms'], case['window_end_ms'] = item['start_ms'], item['end_ms']
    case['prepared_input_sha256'] = item['prepared_input_sha256']
    report['combined_windows'] = [
        {'case_id': c['case_id'], 'proposal_id': c['proposal_id'],
         'start_ms': c['window_start_ms'], 'end_ms': c['window_end_ms'], 'proposal': c['proposal']}
        for c in report['cases'] if c.get('proposal_id') and 'window_start_ms' in c]
    persist()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', default=','.join(CASES))
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    output, report, may_save = args.output.resolve(), None, False
    try:
        with core.lock():
            runner.initialize_django()
            selected = selected_cases(args.cases)
            bundle, inputs, baseline = load_inputs()
            source, baseline_view = validate_source(bundle, baseline)
            org = source['member'].organization
            config = configuration(org, inputs)
            report = json.loads(output.read_text()) if output.exists() else {
                'version': 1, 'experiment': EXPERIMENT, 'run_id': str(uuid.uuid4()), 'max_calls': MAX_CALLS,
                'configuration': config, 'cases': [], 'baseline': baseline_view, 'automatic_retries': False,
                'refinements': False, 'paid_api_enabled': False,
                'limitations': ['One old call versus two candidate windows; not equal-call sampling A/B.',
                    'Current prompt and validation differ from the archived original result.',
                    'Combined windows are a report only; no working instruction is modified.',
                    'One timing per request is not a latency distribution.']}
            refresh_budget(report)
            jobs = experiment_jobs()
            guard(report, selected, config, jobs)
            if {str(j.id) for j in jobs if j.payload.get('benchmark_run_id') == report['run_id']} != {
                    c['job_id'] for c in report['cases']}:
                fail('DENSE_JOB_CHECKPOINT_MISMATCH')
            runner.quota_quote(org, report['run_id'], len(selected))
            report['status'] = 'ready' if args.execute else 'dry_run_validated'
            output.parent.mkdir(parents=True, exist_ok=True)
            previous.atomic_save(output, report)
            may_save = True
            if args.execute:
                for case_id in selected:
                    guard(report, [case_id], configuration(org, inputs), experiment_jobs())
                    run_case(source, next(i for i in inputs if i['case_id'] == case_id), report,
                             lambda: previous.atomic_save(output, report))
                report['status'] = 'completed'
                previous.atomic_save(output, report)
            print(json.dumps({'status': report['status'], 'calls': report['budget']['used_or_held_calls'],
                              'output': str(output)}), flush=True)
    except BaseException as error:
        code = str(error) if isinstance(error, runner.BenchmarkFailure) else getattr(error, 'code', 'DENSE_RUN_FAILED')
        if report is not None and may_save:
            report.update(status='halted', error_code=code)
            previous.atomic_save(output, report)
        print(json.dumps({'status': 'halted', 'error_code': code}), flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
