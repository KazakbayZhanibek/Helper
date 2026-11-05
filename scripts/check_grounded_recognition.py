"""Two-call grounding comparison on the exact previously prepared chopping JPEGs.

Default: read-only preflight and a local report. Only --execute dispatches;
the two fixed case slots cannot be replayed with a new report or after a crash.
Archived preparation hashes document provenance, not the current candidate code.
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
_spec = importlib.util.spec_from_file_location('_grounded_replay_core', ROOT / 'scripts/check_short_actions.py')
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
runner, previous = core.runner, core.previous
core.runner = SimpleNamespace(**vars(runner))
core.runner.execute_case = lambda job, _arm: runner.execute_case(job, 'short_events')
EXPERIMENT, MAX_CALLS = 'recognition-grounded-v1', 2
CASES = ('chopping:anchored-1', 'chopping:anchored-2')
INPUTS = ROOT / 'artifacts/test-results/recognition-dense-inputs-v2.json'
PRIOR_RUN = ROOT / 'artifacts/test-results/recognition-dense-run-v1.json'
BASELINE_INPUTS = ROOT / 'artifacts/test-results/short-actions-inputs-v2.json'
SOURCE_REPORT = ROOT / 'artifacts/test-results/real-video-ab-v1.json'
BASELINE = ROOT / 'artifacts/test-results/grounded-recognition-baseline-v1.json'
CRITERIA = ROOT / 'artifacts/test-results/grounded-recognition-criteria-v1.json'
INPUT_SHA = 'c6f5c7eccb3ab6f2555bcc61d32fab8b85f1900ea47679de48bfc855634ac286'
PRIOR_RUN_SHA = '907e02a3cc6bb431435a23a52179df897563a50f53667b7913cc5ca51369d946'
BASELINE_SHA = 'afe8622d3ec6965f6b6ba1276d163e63d09b771f87d74e69f8efc82cab31f3f3'
CRITERIA_SHA = 'c74753be6edbfdbf062c16ee618d3bccc4c071d5c623b8db0864bf04ae9adcec'


def analysis_options():
    return {'visual_protocol': '3', 'short_action_coverage': 'v1', 'frame_grounding': 'v1'}


core.EXPERIMENT, core.MAX_CALLS, core.CASES = EXPERIMENT, MAX_CALLS, CASES
core.analysis_options = analysis_options
core.CODE_FILES = (*core.CODE_FILES, 'backend/visual_drafts/grounding.py',
                   'backend/visual_drafts/instruction_checks.py', 'backend/ai_budget/services.py',
                   'backend/ai_budget/models.py', 'scripts/compare_real_video.py',
                   'scripts/check_grounded_recognition.py')
digest, fail, selected_cases = core.digest, core.fail, core.selected_cases
experiment_jobs, refresh_budget = core.experiment_jobs, core.refresh_budget


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def frozen_artifacts():
    if (len(BASELINE_SHA) != 64 or len(CRITERIA_SHA) != 64
            or file_sha(BASELINE) != BASELINE_SHA or file_sha(CRITERIA) != CRITERIA_SHA):
        fail('GROUND_FROZEN_REVIEW_CHANGED')
    return json.loads(BASELINE.read_text()), json.loads(CRITERIA.read_text())


def load_inputs():
    """Replay exact saved bytes; no extraction or preparation-code substitution."""
    if file_sha(INPUTS) != INPUT_SHA or file_sha(PRIOR_RUN) != PRIOR_RUN_SHA:
        fail('GROUND_PINNED_ARTIFACT_CHANGED')
    bundle, prior = json.loads(INPUTS.read_text()), json.loads(PRIOR_RUN.read_text())
    if (bundle.get('version') != 2 or bundle.get('model_calls') != 0
            or bundle.get('scope') != 'offline_anchored_dense_frame_preparation_only'
            or bundle['source_report_sha256'] != file_sha(SOURCE_REPORT)
            or bundle['baseline_input_bundle_sha256'] != file_sha(BASELINE_INPUTS)
            or bundle['working_before'] != bundle['working_after']
            or tuple(item['case_id'] for item in bundle['inputs']) != CASES
            or [(item['start_ms'], item['end_ms']) for item in bundle['inputs']] != [(0, 5880), (5880, 11760)]
            or prior.get('experiment') != 'recognition-dense-v1' or prior.get('status') != 'completed'
            or tuple(case['case_id'] for case in prior['cases']) != CASES
            or prior['configuration']['input_bundle_sha256'] != INPUT_SHA):
        fail('GROUND_INPUT_BUNDLE_INVALID')
    # These hashes describe how archived JPEGs were produced. Current code is
    # pinned separately in configuration; comparing both would prevent testing
    # a changed verifier on byte-identical inputs without falsifying history.
    archived_code = bundle.get('preparation_code_sha256')
    if not isinstance(archived_code, dict) or any(not isinstance(value, str) or len(value) != 64 for value in archived_code.values()):
        fail('GROUND_PREPARATION_PROVENANCE_INVALID')
    inputs = []
    for original, baseline in zip(bundle['inputs'], prior['cases']):
        if original['input_sha256'] != digest({key: value for key, value in original.items() if key != 'input_sha256'}):
            fail('GROUND_INPUT_CHANGED')
        if (baseline['status'] != 'done' or baseline['prepared_input_sha256'] != original['input_sha256']
                or baseline['instruction_id'] != bundle['instruction_id']
                or baseline['working_before'] != bundle['working_before']
                or baseline['working_after'] != bundle['working_before']
                or [previous.frame_signature(frame) for frame in baseline['sent_frames']]
                != [previous.frame_signature(frame) for frame in original['frames']]):
            fail('GROUND_PRIOR_INPUT_CHANGED')
        item = {**copy.deepcopy(original), 'clip_id': 'chopping', 'arm': original['case_id'].split(':')[1],
                'prepared_input_sha256': original['input_sha256'], 'working': bundle['working_before'],
                'asset_sha256': bundle['asset_sha256']}
        item['input_sha256'] = digest({key: value for key, value in item.items() if key != 'input_sha256'})
        core.checked_input(item)
        inputs.append(item)
    return bundle, inputs, prior


def validate_source(bundle, prior):
    from visual_drafts.models import VisualProposal
    from visual_drafts.services import proposal_data
    source = previous.validate_source(json.loads(SOURCE_REPORT.read_text()), 'chopping')
    old = source['old_session']
    if (str(old.id) != bundle['source_session_id'] or source['old_case']['job_id'] != bundle['source_job_id']
            or str(old.instruction_id) != bundle['instruction_id'] or str(old.asset_id) != bundle['asset_id']
            or old.asset.sha256 != bundle['asset_sha256'] or source['working_before'] != bundle['working_before']):
        fail('GROUND_SOURCE_CHANGED')
    views = []
    for baseline in prior['cases']:
        proposal = VisualProposal.objects.select_related('session__instruction', 'session__asset').filter(
            pk=baseline['proposal_id'], source_job_id=baseline['job_id'], session_id=baseline['session_id']).first()
        if (not proposal or proposal.applied_at or proposal.session.instruction_id != old.instruction_id
                or proposal.session.asset_id != old.asset_id or proposal.session.state != 'completed'
                or proposal.session.frames_purged_at or proposal.content != baseline['proposal']):
            fail('GROUND_BASELINE_SOURCE_CHANGED')
        frames = list(proposal.session.frames.order_by('timestamp_ms').values(*previous.FRAME_FIELDS))
        if [previous.frame_signature(frame) for frame in frames] != [previous.frame_signature(frame) for frame in baseline['sent_frames']]:
            fail('GROUND_BASELINE_SOURCE_CHANGED')
        views.append({'case_id': baseline['case_id'], 'job_id': baseline['job_id'], 'proposal_id': baseline['proposal_id'],
                      'model_calls': 0, 'wall_ms_original': baseline['wall_ms'],
                      'raw_model_response': baseline['raw_model_response'], 'original_proposal': baseline['proposal'],
                      'current_projection': proposal_data(proposal)})
    validate_baseline_views(views, frozen_artifacts()[0])
    return source, views


def validate_baseline_views(views, frozen):
    if tuple(row['case_id'] for row in views) != CASES or tuple(row['case_id'] for row in frozen['cases']) != CASES:
        fail('GROUND_BASELINE_PROJECTION_CHANGED')
    for view, baseline in zip(views, frozen['cases']):
        if (view['proposal_id'] != baseline['proposal_id']
                or digest(view['original_proposal']) != baseline['original_content_sha256']
                or digest(view['current_projection']) != baseline['current_projection_sha256']):
            fail('GROUND_BASELINE_PROJECTION_CHANGED')


def configuration(organization, inputs):
    bundle, verified, _ = load_inputs()
    frozen_artifacts()
    if verified != inputs:
        fail('GROUND_INPUT_CHANGED')
    config = core.configuration(organization, inputs)
    config.update(input_bundle_sha256=file_sha(INPUTS), prior_run_sha256=file_sha(PRIOR_RUN),
                  baseline_sha256=file_sha(BASELINE), criteria_sha256=file_sha(CRITERIA),
                  archived_preparation_code_sha256=bundle['preparation_code_sha256'],
                  reasoning_effort='medium', frame_strategy='short_events', max_calls=2, max_cost_microusd=0)
    return config


def guard(report, selected, config, jobs):
    if not selected or len(set(selected)) != len(selected):
        fail('GROUND_CASES_INVALID')
    if any(job.payload.get('benchmark_configuration_sha256') != digest(config) for job in jobs):
        fail('GROUND_GLOBAL_CONFIGURATION_CHANGED')
    core.guard(report, selected, config, jobs)


_create_case = core.create_case


def create_case(source, item, run_id):
    from django.db import transaction
    if not source.get('candidate_sha256'):
        fail('GROUND_CONFIGURATION_REQUIRED')
    with transaction.atomic():
        job, session = _create_case(source, item, run_id)
        # The original helper obtains both immutable pins from this isolated
        # module's analysis_options at creation, not through a later mutation.
        if any(session.analysis_budget.get(key) != value or job.payload.get(key) != value
               for key, value in analysis_options().items()):
            fail('GROUND_PROTOCOL_NOT_PINNED')
        job.payload['benchmark_configuration_sha256'] = source['candidate_sha256']
        job.save(update_fields=['payload'])
        window = session.analysis_windows.get()
        window.source_manifest = {**window.source_manifest, 'sampling': 'verified_grounding_replay',
                                  'strategy': 'short_events', 'prepared_input_sha256': item['prepared_input_sha256']}
        window.save(update_fields=['source_manifest', 'updated_at'])
        return job, session


core.create_case = create_case


def run_case(source, item, report, persist):
    from visual_drafts.models import VisualSession, VisualProposal
    from visual_drafts.services import proposal_data
    refresh_budget(report)
    guard(report, [item['case_id']], report['configuration'], experiment_jobs())
    source = {**source, 'candidate_sha256': digest(report['configuration'])}
    core.run_case(source, item, report, persist)
    case = report['cases'][-1]
    receipts = [row for row in report['budget']['reservations'] if str(row['job_id']) == case['job_id']]
    session = VisualSession.objects.get(pk=case['session_id'])
    proposal = VisualProposal.objects.get(pk=case['proposal_id'])
    if (len(receipts) != 1 or receipts[0]['actual_units'] != 1
            or receipts[0]['provider'] != 'codex' or receipts[0]['model'] != 'gpt-6-luna'
            or session.analysis_budget != {'max_calls': 1, 'max_cost_microusd': 0, **analysis_options()}
            or case['raw_model_response'].get('schema_version') != '3-grounded-actions-v1'
            or proposal.content.get('frame_grounding', {}).get('version') != 'v1'
            or len(proposal.content['frame_grounding']['batches']) != 1):
        fail('GROUND_RESULT_OR_RECEIPT_INVALID')
    case.update(window_start_ms=item['start_ms'], window_end_ms=item['end_ms'],
                prepared_input_sha256=item['prepared_input_sha256'], session_budget=session.analysis_budget,
                delivered_projection=proposal_data(proposal))
    report['combined_windows'] = [
        {'case_id': row['case_id'], 'proposal_id': row['proposal_id'], 'start_ms': row['window_start_ms'],
         'end_ms': row['window_end_ms'], 'proposal': row['proposal']}
        for row in report['cases'] if row.get('proposal_id') and 'window_start_ms' in row]
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
            bundle, inputs, prior = load_inputs()
            baseline, criteria = frozen_artifacts()
            source, views = validate_source(bundle, prior)
            org = source['member'].organization
            config = configuration(org, inputs)
            report = json.loads(output.read_text()) if output.exists() else {
                'version': 1, 'experiment': EXPERIMENT, 'run_id': str(uuid.uuid4()), 'max_calls': MAX_CALLS,
                'configuration': config, 'cases': [], 'baseline': baseline, 'criteria': criteria,
                'baseline_current_views': views, 'automatic_retries': False, 'refinements': False,
                'paid_api_enabled': False, 'limitations': [
                    'Same 24 prepared JPEGs and two calls as the archived dense baseline; prompt/schema/verifier changed.',
                    'Frame facts and requirements come from one model; consistency is not independent visual truth.',
                    'Archived preparation-code hashes are provenance; current candidate code is pinned separately.',
                    'Combined windows are a report only; no working instruction is modified.',
                    'One timing per request is not a latency distribution.']}
            refresh_budget(report)
            jobs = experiment_jobs()
            guard(report, selected, config, jobs)
            if {str(job.id) for job in jobs if job.payload.get('benchmark_run_id') == report['run_id']} != {
                    case['job_id'] for case in report['cases']}:
                fail('GROUND_JOB_CHECKPOINT_MISMATCH')
            runner.quota_quote(org, report['run_id'], len(selected))
            report['status'] = 'ready' if args.execute else 'dry_run_validated'
            output.parent.mkdir(parents=True, exist_ok=True)
            previous.atomic_save(output, report)
            may_save = True
            if args.execute:
                for case_id in selected:
                    guard(report, [case_id], configuration(org, inputs), experiment_jobs())
                    run_case(source, next(item for item in inputs if item['case_id'] == case_id), report,
                             lambda: previous.atomic_save(output, report))
                report['status'] = 'completed'
                previous.atomic_save(output, report)
            print(json.dumps({'status': report['status'], 'calls': report['budget']['used_or_held_calls'],
                              'output': str(output)}), flush=True)
    except BaseException as error:
        code = str(error) if isinstance(error, runner.BenchmarkFailure) else getattr(error, 'code', 'GROUND_RUN_FAILED')
        if report is not None and may_save:
            report.update(status='halted', error_code=code)
            previous.atomic_save(output, report)
        print(json.dumps({'status': 'halted', 'error_code': code}), flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
