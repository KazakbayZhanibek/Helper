"""Four-call teaching regression using exact previously sent real-video JPEGs.

Dry run validates without model calls. --execute creates independent proposals,
never applies them, and never replays an attempted case. Reuse the same output
report to execute additional unattempted clips under the same frozen candidate.
"""
import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import compare_real_video as runner

CLIPS = ('kneading', 'screwdriving', 'chopping', 'sewing')
MAX_CALLS = 4
EXPERIMENT = 'teaching-video-v1'
EXPERIMENTS = frozenset({'teaching-video-v1', 'semantic-grounding-v1'})
# One lock serializes all allowlisted experiments, while each has its own
# immutable job tag and four-call allowance in the actual accounting ledger.
EXPERIMENT_LOCK = ROOT / 'artifacts/test-results/.teaching-video-v1.lock'
FRAME_FIELDS = ('id', 'timestamp_ms', 'width', 'height', 'size_bytes', 'sha256')
CODE_FILES = ('backend/visual_drafts/provider.py', 'backend/visual_drafts/action_evidence.py',
              'backend/visual_drafts/teaching.py', 'backend/visual_drafts/review.py', 'backend/visual_drafts/planning.py',
              'backend/visual_drafts/services.py', 'backend/visual_drafts/worker.py', 'backend/visual_drafts/review_worker.py',
              'backend/visual_drafts/models.py', 'backend/visual_drafts/migrations/0005_semantic_review.py',
              'backend/media_pipeline/telemetry.py',
              'backend/ai_drafts/codex_provider.py', 'scripts/check_teaching_video.py')


def fail(code):
    raise runner.BenchmarkFailure(code)


def checked_experiment(value):
    if not isinstance(value, str) or value not in EXPERIMENTS:
        fail('REGRESSION_EXPERIMENT_INVALID')
    return value


def fingerprint_working(instruction):
    from core.models import WorkingCopy, Instruction
    working = WorkingCopy.objects.get(instruction=instruction)
    value = WorkingCopy.objects.filter(pk=working.pk).values().get()
    value['steps'] = list(working.steps.values())
    value['current_version_id'] = Instruction.objects.get(pk=instruction.pk).current_version_id
    digest = hashlib.sha256(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()
    return {'revision': working.revision, 'sha256': digest, 'workflow': working.workflow, 'is_open': working.is_open}


def validate_source(source_report, clip_id):
    from visual_drafts.models import VisualSession
    from visual_drafts.services import safe_frame_path
    from media_pipeline.storage import private_path
    matches = [case for case in source_report['cases'] if case['clip_id'] == clip_id and case['strategy'] == 'events']
    if len(matches) != 1 or matches[0]['status'] != 'done':
        fail('REGRESSION_SOURCE_CASE_INVALID')
    old_case = matches[0]
    old = VisualSession.objects.select_related('instruction__source_asset', 'asset', 'created_by', 'organization').get(pk=old_case['session_id'])
    if (old.mode != 'upload' or old.state != 'completed' or old.frames_purged_at or not old.consent_images
            or old.instruction.lifecycle != 'active' or old.instruction.source_asset_id != old.asset_id
            or old.asset.status != 'ready' or old.organization.status != 'active'):
        fail('REGRESSION_SOURCE_CHANGED')
    source = next(item for item in source_report['sources'] if item['clip_id'] == clip_id)
    if old.asset.sha256 != source['sha256'] or str(old.instruction_id) != source['instruction_id']:
        fail('REGRESSION_SOURCE_CHANGED')
    with private_path(old.asset.storage_key).open('rb') as file:
        if hashlib.file_digest(file, 'sha256').hexdigest() != source['sha256']:
            fail('REGRESSION_PLAYBACK_CHANGED')
    working = fingerprint_working(old.instruction)
    if not working['is_open'] or working['workflow'] != 'draft':
        fail('REGRESSION_WORKING_NOT_DRAFT')
    if old.instruction.working_copy.language != old.language:
        fail('REGRESSION_LANGUAGE_CHANGED')
    windows = list(old.analysis_windows.all())
    if len(windows) != 1 or windows[0].state != 'completed':
        fail('REGRESSION_WINDOW_INVALID')
    frames = list(old.frames.order_by('timestamp_ms'))
    observed = [{key: str(getattr(frame, key)) if key == 'id' else getattr(frame, key) for key in FRAME_FIELDS} for frame in frames]
    if len(frames) != 12 or observed != old_case['sent_frames'] or set(windows[0].frame_ids) != {str(frame.id) for frame in frames}:
        fail('REGRESSION_EVIDENCE_CHANGED')
    evidence = []
    for frame in frames:
        path = safe_frame_path(frame.storage_key)
        if path.stat().st_size != frame.size_bytes or not 0 < frame.size_bytes <= 524288:
            fail('REGRESSION_FRAME_INVALID')
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != frame.sha256:
            fail('REGRESSION_FRAME_CHANGED')
        evidence.append((frame, content))
    member = runner.resolve_member(str(old.organization_id), old.created_by.email)
    return {'clip_id': clip_id, 'old_case': old_case, 'old_session': old, 'member': member,
            'evidence': evidence, 'working_before': working, 'old_window': windows[0]}


def create_replay(source, run_id, experiment=EXPERIMENT):
    from django.db import transaction
    from django.utils import timezone
    from core.models import Organization, WorkingCopy
    from media_pipeline.models import ProcessingJob
    from media_pipeline.worker import LEASE_SECONDS
    from visual_drafts.models import VisualSession, VisualAnalysisWindow
    from visual_drafts.services import persist_frame, safe_frame_path
    experiment = checked_experiment(experiment)
    old, member = source['old_session'], source['member']
    written = []
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=member.organization_id)
            working = WorkingCopy.objects.select_for_update().get(instruction_id=old.instruction_id)
            if fingerprint_working(old.instruction) != source['working_before']:
                fail('REGRESSION_WORKING_CHANGED')
            old.instruction.refresh_from_db()
            old.asset.refresh_from_db()
            if old.instruction.source_asset_id != old.asset_id or old.asset.status != 'ready':
                fail('REGRESSION_SOURCE_CHANGED')
            if ProcessingJob.objects.filter(instruction_id=old.instruction_id, state__in=['queued', 'running']).exists():
                fail('REGRESSION_JOB_ALREADY_ACTIVE')
            now = timezone.now()
            session = VisualSession.objects.create(organization=member.organization, instruction=old.instruction,
                asset=old.asset, created_by=member.user, mode='upload', state='active', consent_images=True,
                base_working_revision=working.revision, language=working.language, expires_at=now + timedelta(hours=1),
                window_start_ms=old.window_start_ms, window_end_ms=old.window_end_ms, analysis_scope='window',
                analysis_budget={'max_calls': 1, 'max_cost_microusd': 0, 'visual_protocol': '3'})
            clones = []
            for index, (frame, content) in enumerate(source['evidence'], 1):
                clone = persist_frame(session, sequence=index, timestamp_ms=frame.timestamp_ms,
                    coverage_start_ms=frame.coverage_start_ms, coverage_end_ms=frame.coverage_end_ms,
                    content=content, width=frame.width, height=frame.height, idempotency_key=f'replay-{index}',
                    request_sha256=frame.sha256)
                written.append(safe_frame_path(clone.storage_key))
                if clone.sha256 != frame.sha256 or clone.size_bytes != frame.size_bytes:
                    fail('REGRESSION_COPY_CHANGED')
                clones.append(clone)
            manifest = {'sampling_version': 'verified-evidence-replay-v1', 'sparse_sampling': True,
                'asset_id': str(old.asset_id), 'source_sha256': old.asset.sha256,
                'start_ms': old.window_start_ms, 'end_ms': old.window_end_ms, 'frame_count': len(clones),
                'requested_count': 12, 'replay_source_session_id': str(old.id),
                'replay_source_job_id': source['old_case']['job_id'],
                'frames': [{key: str(getattr(frame, key)) if key == 'id' else getattr(frame, key) for key in FRAME_FIELDS} for frame in clones],
                'processing': {'strategy': 'verified_evidence_replay', 'ffmpeg_processes': 0, 'ffprobe_processes': 0}}
            VisualAnalysisWindow.objects.create(organization=member.organization, session=session, ordinal=1,
                start_ms=old.window_start_ms, end_ms=old.window_end_ms,
                frame_ids=[str(frame.id) for frame in clones], source_manifest=manifest)
            job = ProcessingJob.objects.create(organization=member.organization, instruction=old.instruction,
                asset=old.asset, requested_by=member.user, kind='analyze_video', state='running', stage='starting',
                attempts=1, max_attempts=1, started_at=now, heartbeat_at=now, lease_token=uuid.uuid4(),
                lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
                payload={'visual_session_id': str(session.id), 'visual_protocol': '3', 'benchmark_experiment': experiment, 'benchmark_run_id': run_id})
            return job, session
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise


def selected_clips(value):
    values = tuple(item.strip() for item in value.split(','))
    if not values or len(set(values)) != len(values) or any(item not in CLIPS for item in values):
        fail('REGRESSION_CLIPS_INVALID')
    return values


def frame_signature(frame):
    return tuple(frame[key] for key in ('timestamp_ms', 'width', 'height', 'size_bytes', 'sha256'))


def remap_candidate(candidate, old_frames, new_frames):
    """Map only explicit evidence fields, preserving every other untrusted claim."""
    if (len(old_frames) != 12 or len(new_frames) != 12
            or len({str(frame['id']) for frame in old_frames}) != 12
            or len({str(frame['id']) for frame in new_frames}) != 12
            or len({frame_signature(frame) for frame in old_frames}) != 12
            or [frame_signature(frame) for frame in old_frames] != [frame_signature(frame) for frame in new_frames]):
        fail('REGRESSION_REVIEW_EVIDENCE_CHANGED')
    mapping = {str(old['id']): str(new['id']) for old, new in zip(old_frames, new_frames)}
    value = copy.deepcopy(candidate)
    try:
        if value.get('schema_version') != '3':
            raise ValueError()
        for observation in value['observations']:
            for key in ('before_frame_id', 'after_frame_id'):
                if observation[key] is not None:
                    observation[key] = mapping[observation[key]]
            for key in ('action_frame_ids', 'evidence_frame_ids'):
                observation[key] = [mapping[ref] for ref in observation[key]]
        for gap in value['gaps']:
            gap['evidence_frame_ids'] = [mapping[ref] for ref in gap['evidence_frame_ids']]
    except (KeyError, TypeError, ValueError, AttributeError):
        fail('REGRESSION_REVIEW_CANDIDATE_INVALID')
    return value


def load_review_context(path):
    try:
        raw = path.read_bytes()
        prior = json.loads(raw)
        cases = {}
        for case in prior['cases']:
            if 'raw_model_response' not in case:
                continue
            clip = case['clip_id']
            if clip not in CLIPS or clip in cases:
                raise ValueError()
            candidate, frames = case['raw_model_response'], case['sent_frames']
            remap_candidate(candidate, frames, frames)
            cases[clip] = {'candidate': candidate, 'sent_frames': frames, 'source_job_id': case['job_id']}
        if not cases:
            raise ValueError()
        return {'source_report_sha256': hashlib.sha256(raw).hexdigest(), 'cases': cases}
    except (OSError, KeyError, TypeError, ValueError, AttributeError):
        fail('REGRESSION_REVIEW_REPORT_INVALID')


def review_preflight(source, reviews):
    """Validate the exact candidate and input bounds locally, before creating a job."""
    from visual_drafts.review import VisualReviewProvider
    item = reviews['cases'].get(source['clip_id'])
    if item is None:
        fail('REGRESSION_REVIEW_CLIP_MISSING')
    candidate = remap_candidate(item['candidate'], item['sent_frames'], source['old_case']['sent_frames'])
    frames = [{'id': str(frame.id), 'sequence': frame.sequence, 'timestamp_ms': frame.timestamp_ms,
               'sha256': frame.sha256, 'bytes': raw} for frame, raw in source['evidence']]
    reviewer = VisualReviewProvider(source['member'].organization, candidate=candidate)
    reviewer.prepare_request(language=source['old_session'].language, mode='upload', frames=frames)
    return candidate


def configuration(organization, reviews=None):
    from visual_drafts.provider import request_contract
    config = runner.safe_config(organization)
    if config['model'] != 'gpt-6-luna':
        fail('REGRESSION_MODEL_CHANGED')
    # Canonical IDs make this hash stable while actual schemas are rebound to
    # fresh frame IDs for each request. The same maximum frame count is used.
    frames = [{'id': str(uuid.UUID(int=index)), 'sequence': index, 'timestamp_ms': index * 1000} for index in range(1, 13)]
    system, schema = request_contract('3', 'ru', 'upload', frames)
    config.update(visual_protocol='3', prompt_sha256=hashlib.sha256(system.encode()).hexdigest(),
                  schema_sha256=hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
                  code_sha256={name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in CODE_FILES if (ROOT / name).is_file()})
    if reviews is not None:
        from visual_drafts.review import request_review_contract
        candidates = {}
        for clip, item in sorted(reviews['cases'].items()):
            canonical = [{**frame, 'id': str(uuid.UUID(int=index)), 'sequence': index} for index, frame in enumerate(item['sent_frames'], 1)]
            candidate = remap_candidate(item['candidate'], item['sent_frames'], canonical)
            review_system, review_schema, request = request_review_contract(candidate['language'], 'upload', canonical, candidate)
            candidates[clip] = {'source_job_id': item['source_job_id'],
                                'raw_candidate_sha256': hashlib.sha256(json.dumps(item['candidate'], sort_keys=True).encode()).hexdigest(),
                                'canonical_request_sha256': hashlib.sha256(json.dumps({'system': review_system, 'schema': review_schema, 'request': request}, sort_keys=True).encode()).hexdigest()}
        config['review'] = {'mode': 'review_only', 'source_report_sha256': reviews['source_report_sha256'], 'candidates': candidates}
    return config


def experiment_jobs(experiment=EXPERIMENT):
    from media_pipeline.models import ProcessingJob
    experiment = checked_experiment(experiment)
    return list(ProcessingJob.objects.filter(payload__benchmark_experiment=experiment).values_list('id', flat=True))


def refresh_budget(report):
    report['budget'] = runner.ledger(experiment_jobs(checked_experiment(report.get('experiment'))))
    return report['budget']


def guard_resume(report, clips, current_config, job_ids=None, *, experiment=EXPERIMENT):
    """Fail closed before any new model dispatch or replay of a stored case."""
    experiment = checked_experiment(experiment)
    if (report.get('version') != 1 or report.get('experiment') != experiment
            or report.get('max_calls') != MAX_CALLS or report.get('configuration') != current_config):
        fail('REGRESSION_CONFIGURATION_CHANGED')
    attempted = [case['clip_id'] for case in report['cases']]
    if len(set(attempted)) != len(attempted) or any(clip in attempted for clip in clips):
        fail('REGRESSION_CASE_ALREADY_ATTEMPTED')
    budget = report['budget']
    if budget['held_calls'] or any(case.get('status') == 'before_dispatch' for case in report['cases']):
        fail('REGRESSION_OUTCOME_UNRESOLVED')
    # A prepared but never dispatched job also consumes a test slot, preventing
    # a crash or report replacement from creating an unbounded number of tries.
    attempts = len(job_ids) if job_ids is not None else budget['used_or_held_calls']
    if max(attempts, budget['used_or_held_calls']) + len(clips) > MAX_CALLS:
        fail('REGRESSION_CALL_LIMIT')


@contextmanager
def capture_validation(case):
    """Diagnostic-only boundary for these licensed public benchmark frames.

    Forward the original value and validation result unchanged. Recording a
    stable failure code does not add a model call or repair rejected content.
    """
    from ai_drafts.validation import InvalidModelOutput
    from visual_drafts import worker
    original = worker.validate

    def captured(value, *args, **kwargs):
        case['raw_model_response'] = copy.deepcopy(value)
        try:
            result = original(value, *args, **kwargs)
        except InvalidModelOutput as error:
            code = str(error)
            case['validation'] = {'status': 'rejected', 'code': code if re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', code) else 'VISUAL_OUTPUT_INVALID'}
            raise
        except (TypeError, ValueError, RecursionError):
            case['validation'] = {'status': 'rejected', 'code': 'VISUAL_OUTPUT_INVALID'}
            raise
        case['validation'] = {'status': 'passed'}
        return result

    worker.validate = captured
    try:
        yield
    finally:
        worker.validate = original


@contextmanager
def use_reviewer(candidate):
    """One already-budgeted worker call, with the prior candidate as extra data."""
    if candidate is None:
        yield
        return
    from visual_drafts import worker
    from visual_drafts.review import VisualReviewProvider
    original = worker.VisualProvider
    worker.VisualProvider = lambda organization: VisualReviewProvider(organization, candidate=candidate)
    try:
        yield
    finally:
        worker.VisualProvider = original


def run(sources, report, persist, reviews=None):
    from media_pipeline.jobs import job_data
    from visual_drafts.models import VisualProposal
    experiment = checked_experiment(report.get('experiment'))
    for source in sources:
        refresh_budget(report)
        config = configuration(source['member'].organization, reviews) if reviews is not None else configuration(source['member'].organization)
        guard_resume(report, [source['clip_id']], config, experiment_jobs(experiment), experiment=experiment)
        review_source = reviews['cases'].get(source['clip_id']) if reviews is not None else None
        if reviews is not None and review_source is None:
            fail('REGRESSION_REVIEW_CLIP_MISSING')
        reviewed_input = review_preflight(source, reviews) if review_source is not None else None
        job, session = create_replay(source, report['run_id'], experiment)
        case = {'clip_id': source['clip_id'], 'old_session_id': str(source['old_session'].id),
                'old_job_id': source['old_case']['job_id'], 'old_proposal': source['old_case']['proposal'],
                'job_id': str(job.id), 'session_id': str(session.id), 'max_calls': 1,
                'instruction_id': str(job.instruction_id), 'working_before': source['working_before'],
                'url': f'http://127.0.0.1:4173/o/{job.organization_id}/instructions/{job.instruction_id}/edit',
                'status': 'before_dispatch'}
        candidate = None
        if review_source is not None:
            cloned_frames = list(session.frames.order_by('timestamp_ms').values(*FRAME_FIELDS))
            candidate = remap_candidate(reviewed_input, source['old_case']['sent_frames'], cloned_frames)
            case['review'] = {'mode': 'review_only', 'source_report_sha256': reviews['source_report_sha256'],
                              'source_job_id': review_source['source_job_id'], 'candidate': candidate}
        report['cases'].append(case)
        report['status'] = 'running'
        persist()  # Persist the exact job before its first possible dispatch.
        print(json.dumps({'clip': source['clip_id'], 'status': 'started', 'job_id': str(job.id)}), flush=True)
        interrupted = False
        try:
            with capture_validation(case), use_reviewer(candidate):
                case['wall_ms'] = runner.execute_case(job, 'events')
        except BaseException:
            interrupted = True
        finally:
            job.refresh_from_db()
            case['job'], case['status'] = job_data(job), job.state
            case['sent_frames'] = list(session.frames.order_by('timestamp_ms').values(*FRAME_FIELDS))
            signature = lambda frames: [(item['timestamp_ms'], item['width'], item['height'], item['size_bytes'], item['sha256']) for item in frames]
            case['identical_images_and_times'] = signature(case['sent_frames']) == signature(source['old_case']['sent_frames'])
            proposal = VisualProposal.objects.filter(source_job_id=job.id).first()
            if proposal:
                case['proposal_id'], case['proposal'] = str(proposal.id), proposal.content
            case['working_after'] = fingerprint_working(source['old_session'].instruction)
            case['windows'] = list(session.analysis_windows.values('id', 'state', 'error_code', 'start_ms', 'end_ms'))
            refresh_budget(report)
            persist()
        receipts = [row for row in report['budget']['reservations'] if str(row['job_id']) == str(job.id)]
        if (interrupted or job.state != 'done' or len(receipts) != 1 or receipts[0]['state'] != 'settled'
                or receipts[0]['unit'] != 'calls' or receipts[0]['max_units'] != 1
                or len(case['windows']) != 1 or case['windows'][0]['state'] != 'completed'
                or not case['identical_images_and_times'] or case['working_before'] != case['working_after']
                or not proposal or proposal.applied_at or proposal.content.get('schema_version') != '3'
                or report['budget']['used_or_held_calls'] > MAX_CALLS):
            fail('REGRESSION_STOPPED_NO_RETRY')
        print(json.dumps({'clip': source['clip_id'], 'status': 'done', 'calls': report['budget']['used_or_held_calls']}), flush=True)
    report['status'] = 'completed'
    report['working_copies_unchanged'] = True
    persist()


def atomic_save(path, report):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as file:
        json.dump(report, file, ensure_ascii=False, indent=2, default=str)
        file.write('\n')
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


@contextmanager
def report_lock(path, experiment=EXPERIMENT):
    # Advisory OS lock is released on exit/crash; it does not need stale-lock
    # deletion. A concurrent caller cannot read a half-updated report and spend.
    checked_experiment(experiment)
    EXPERIMENT_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with EXPERIMENT_LOCK.open('a') as file:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail('REGRESSION_ALREADY_RUNNING')
        try:
            yield
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-report', type=Path, default=ROOT / 'artifacts/test-results/real-video-ab-v1.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--experiment', choices=sorted(EXPERIMENTS), default=EXPERIMENT,
                        help='Explicitly authorized, independently capped experiment; old allowance is never reset.')
    parser.add_argument('--clips', default=','.join(CLIPS), help='Only unattempted clips; comma-separated.')
    parser.add_argument('--review-report', type=Path, help='Prior raw v3 responses for the semantic-grounding-v1 review-only experiment.')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with report_lock(output, args.experiment):
            report = None
            persist_failure = False
            try:
                clips = selected_clips(args.clips)
                if args.experiment == 'semantic-grounding-v1' and args.review_report is None:
                    fail('REGRESSION_REVIEW_REPORT_REQUIRED')
                if args.review_report is not None and args.experiment != 'semantic-grounding-v1':
                    fail('REGRESSION_REVIEW_EXPERIMENT_REQUIRED')
                reviews = load_review_context(args.review_report) if args.review_report is not None else None
                if reviews is not None and any(clip not in reviews['cases'] for clip in clips):
                    fail('REGRESSION_REVIEW_CLIP_MISSING')
                prior = json.loads(args.source_report.read_text())
                source_hash = hashlib.sha256(args.source_report.read_bytes()).hexdigest()
                runner.initialize_django()
                sources = [validate_source(prior, clip_id) for clip_id in clips]
                if len({source['member'].organization_id for source in sources}) != 1:
                    fail('REGRESSION_SOURCE_SET_INVALID')
                if reviews is not None:
                    for source in sources:
                        review_preflight(source, reviews)
                config = configuration(sources[0]['member'].organization, reviews) if reviews is not None else configuration(sources[0]['member'].organization)
                if output.exists():
                    report = json.loads(output.read_text())
                    if report.get('experiment') != args.experiment:
                        fail('REGRESSION_EXPERIMENT_MISMATCH')
                    if report.get('source_report_sha256') != source_hash:
                        fail('REGRESSION_SOURCE_REPORT_CHANGED')
                else:
                    persist_failure = True
                    report = {'version': 1, 'experiment': args.experiment, 'run_id': str(uuid.uuid4()),
                              'status': 'validating', 'max_calls': MAX_CALLS, 'configuration': config,
                              'source_report_sha256': source_hash, 'refinements': False,
                              'automatic_retries': False, 'paid_api_enabled': False, 'cases': []}
                refresh_budget(report)
                guard_resume(report, clips, config, experiment_jobs(args.experiment), experiment=args.experiment)
                # Prevent an orphan job from being hidden by editing/replacing
                # the report after preparation but before its durable checkpoint.
                from media_pipeline.models import ProcessingJob
                run_jobs = {str(value) for value in ProcessingJob.objects.filter(payload__benchmark_run_id=report['run_id']).values_list('id', flat=True)}
                if run_jobs != {case['job_id'] for case in report['cases']}:
                    fail('REGRESSION_JOB_CHECKPOINT_MISMATCH')
                if any(config[key] != prior['configuration'][key] for key in ('provider', 'model')):
                    fail('REGRESSION_MODEL_CHANGED')
                runner.quota_quote(sources[0]['member'].organization, report['run_id'], len(clips))
                report['validated_inputs'] = [{'clip_id': source['clip_id'], 'old_job_id': source['old_case']['job_id'],
                                                'frame_count': len(source['evidence']), 'working': source['working_before']} for source in sources]
                report['status'] = 'ready' if args.execute else 'dry_run_validated'
                report.pop('error_code', None)
                atomic_save(output, report)
                persist_failure = True
                if args.execute:
                    run(sources, report, lambda: atomic_save(output, report), reviews)
                print(json.dumps({'status': report['status'], 'calls': report['budget']['used_or_held_calls'], 'output': str(output)}), flush=True)
            except BaseException as error:
                code = str(error) if isinstance(error, runner.BenchmarkFailure) else getattr(error, 'code', 'REGRESSION_FAILED')
                # Refused resume is read-only. Never rewrite an older report
                # merely because the caller selected another experiment/config.
                if report is not None and persist_failure:
                    report['status'], report['error_code'] = 'halted', code
                    atomic_save(output, report)
                print(json.dumps({'status': 'halted', 'error_code': code}), flush=True)
                raise SystemExit(1) from None
    except runner.BenchmarkFailure as error:
        print(json.dumps({'status': 'halted', 'error_code': str(error)}), flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
