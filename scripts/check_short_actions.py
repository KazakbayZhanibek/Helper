"""Four-call comparison of short-action frame selection, never auto-applied.

--prepare exports and measures local frames only. With --inputs, the default
is a no-call preflight; --execute is required to dispatch a model. The fixed
experiment has four unique clip/arm slots across all reports and restarts.
"""
import argparse
import copy
import fcntl
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import compare_real_video as runner
import check_teaching_video as previous

EXPERIMENT = 'short-actions-v1'
MAX_CALLS = 4
CLIPS = ('kneading', 'chopping')
ARMS = ('events', 'short_events')
CASES = tuple(f'{clip}:{arm}' for clip in CLIPS for arm in ARMS)
CRITERIA_PATH = ROOT / 'artifacts/test-results/short-actions-criteria-v1.json'
LOCK_PATH = previous.EXPERIMENT_LOCK  # All local inference runners serialize.
CODE_FILES = tuple(dict.fromkeys((*previous.CODE_FILES,
    'backend/media_pipeline/frames.py', 'backend/media_pipeline/event_sampling.py',
    'backend/visual_drafts/short_actions.py', 'backend/config/settings.py', 'scripts/check_short_actions.py')))


def fail(code):
    raise runner.BenchmarkFailure(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def analysis_options():
    return {'visual_protocol': '3', 'short_action_coverage': 'v1'}


def selected_cases(value):
    selected = tuple(part.strip() for part in value.split(','))
    if not selected or len(set(selected)) != len(selected) or any(item not in CASES for item in selected):
        fail('SHORT_CASES_INVALID')
    return selected


def sampling_coverage(actions, times):
    """Image availability only; no statement about recognized or visible action."""
    return [{'id': action['id'], 'label': action['label'], 'importance': action['importance'],
             'start_ms': action['start_ms'], 'end_ms': action['end_ms'],
             'samples_inside_ms': [t for t in times if action['start_ms'] <= t < action['end_ms']],
             'nearest_before_ms': max((t for t in times if t < action['start_ms']), default=None),
             'nearest_after_ms': min((t for t in times if t >= action['end_ms']), default=None)}
            for action in actions]


class LocalContext:
    """Read-only DB reference for the local extractor, no worker or AI execution."""
    def __init__(self, source):
        self.job_id = source['old_case']['job_id']
        self.lease_token = uuid.uuid4()
        self.source = source

    def check(self):
        from media_pipeline.models import ProcessingJob
        if not ProcessingJob.objects.filter(pk=self.job_id, asset_id=self.source['old_session'].asset_id,
                                            organization_id=self.source['member'].organization_id).exists():
            fail('SHORT_SOURCE_JOB_CHANGED')

    def run(self, command, timeout, **kwargs):
        from media_pipeline.exceptions import ProcessingFailure
        try:
            return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout, **kwargs)
        except (subprocess.SubprocessError, OSError):
            raise ProcessingFailure('SHORT_LOCAL_EXTRACTION_FAILED', 'Локальная подготовка теста остановлена.') from None


def _write_frame(path, raw):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(raw)


def contact_sheet(frames, target):
    from PIL import Image, ImageDraw
    width, height, label = 320, 180, 27
    sheet = Image.new('RGB', (width * 3, (height + label) * 4), '#eeeeee')
    draw = ImageDraw.Draw(sheet)
    for index, frame in enumerate(frames):
        with Image.open(frame['file']) as original:
            picture = original.convert('RGB')
            picture.thumbnail((width, height))
            x, y = index % 3 * width, index // 3 * (height + label)
            sheet.paste(picture, (x + (width - picture.width) // 2, y))
            draw.text((x + 8, y + height + 6), f"{index + 1:02d} | {frame['timestamp_ms']} ms", fill='black')
    sheet.save(target, format='JPEG', quality=90)
    target.chmod(0o600)


def prepare(source_report_path, reference_path, output):
    from django.db import connection
    from media_pipeline.frames import extract_video_frames
    from visual_drafts.services import decoded_jpeg
    if output.exists():
        fail('SHORT_OUTPUT_EXISTS')
    original = json.loads(source_report_path.read_text())
    reference = json.loads(reference_path.read_text())
    folder = output.with_name(output.stem + '-frames')
    folder.mkdir(mode=0o700)  # Exclusive: never overwrite an earlier frame bundle.
    result = {'version': 1, 'experiment': EXPERIMENT, 'scope': 'local_frame_preparation_only',
              'model_calls': 0, 'source_report_sha256': hashlib.sha256(source_report_path.read_bytes()).hexdigest(),
              'reference_sha256': hashlib.sha256(reference_path.read_bytes()).hexdigest(), 'inputs': [],
              'criteria': {'path': str(CRITERIA_PATH), 'sha256': hashlib.sha256(CRITERIA_PATH.read_bytes()).hexdigest()},
              'preparation_code_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                          for name in ('backend/media_pipeline/frames.py', 'backend/media_pipeline/event_sampling.py')},
              'limitations': ['Sampling an interval does not establish recognition or visible action.',
                             'One local extraction timing is not a latency distribution.',
                             'Saved events JPEGs exclude extraction time; timing is not an A/B speed test.']}
    # This task only uses SQLite locally. A read-only DB session is an extra
    # guarantee that local preparation cannot create jobs or spend receipts.
    if connection.vendor != 'sqlite':
        fail('SHORT_LOCAL_SQLITE_REQUIRED')
    with connection.cursor() as cursor:
        cursor.execute('PRAGMA query_only = ON')
    try:
        for clip in CLIPS:
            source = previous.validate_source(original, clip)
            old = source['old_session']
            actions = next(item['actions'] for item in reference['clips'] if item['id'] == clip)
            for arm in ARMS:
                frame_dir = folder / f'{clip}-{arm}'
                frame_dir.mkdir(mode=0o700)
                frames, manifest = [], {}
                started = time.perf_counter()
                if arm == 'events':
                    samples = [(raw, {'timestamp_ms': frame.timestamp_ms, 'width': frame.width, 'height': frame.height,
                                     'coverage_start_ms': frame.coverage_start_ms, 'coverage_end_ms': frame.coverage_end_ms})
                               for frame, raw in source['evidence']]
                    manifest = copy.deepcopy(source['old_window'].source_manifest)
                else:
                    with extract_video_frames(old.asset, LocalContext(source), start_ms=old.window_start_ms,
                                              end_ms=old.window_end_ms, count=12, strategy=arm) as batch:
                        manifest = batch.manifest()
                        times = [frame.timestamp_ms for frame in batch.frames]
                        bounds = [old.window_start_ms, *[(a + b) // 2 for a, b in zip(times, times[1:])], old.window_end_ms]
                        samples = []
                        for index, frame in enumerate(batch.frames):
                            raw, width, height = decoded_jpeg(frame.path.read_bytes(), max_input_bytes=1024 * 1024)
                            samples.append((raw, {'timestamp_ms': frame.timestamp_ms, 'width': width, 'height': height,
                                                  'coverage_start_ms': bounds[index], 'coverage_end_ms': bounds[index + 1]}))
                wall_ms = round((time.perf_counter() - started) * 1000, 3)
                if len(samples) != 12:
                    fail('SHORT_FRAME_COUNT_CHANGED')
                for index, (raw, meta) in enumerate(samples, 1):
                    path = frame_dir / f'{index:02d}.jpg'
                    _write_frame(path, raw)
                    frames.append({**meta, 'file': str(path), 'sequence': index,
                                   'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw)})
                sheet = frame_dir / 'contact-sheet.jpg'
                contact_sheet(frames, sheet)
                item = {'case_id': f'{clip}:{arm}', 'clip_id': clip, 'arm': arm,
                        'source_session_id': str(old.id), 'source_job_id': source['old_case']['job_id'],
                        'instruction_id': str(old.instruction_id), 'asset_sha256': old.asset.sha256,
                        'start_ms': old.window_start_ms, 'end_ms': old.window_end_ms,
                        'working': source['working_before'], 'frames': frames, 'manifest': manifest,
                        'local_preparation_ms': wall_ms, 'contact_sheet': str(sheet),
                        'coverage': sampling_coverage(actions, [frame['timestamp_ms'] for frame in frames])}
                item['input_sha256'] = digest(item)
                result['inputs'].append(item)
                print(json.dumps({'case': item['case_id'], 'local_ms': wall_ms, 'model_calls': 0}), flush=True)
    finally:
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA query_only = OFF')
    previous.atomic_save(output, result)
    return result


def checked_input(item):
    from PIL import Image
    snapshot = {key: value for key, value in item.items() if key != 'input_sha256'}
    if (item.get('case_id') not in CASES or item.get('input_sha256') != digest(snapshot)
            or item['case_id'] != f"{item.get('clip_id')}:{item.get('arm')}"):
        fail('SHORT_INPUT_CHANGED')
    frames = item['frames']
    if len(frames) != 12:
        fail('SHORT_FRAME_COUNT_CHANGED')
    times = [frame['timestamp_ms'] for frame in frames]
    bounds = [item['start_ms'], *[(a + b) // 2 for a, b in zip(times, times[1:])], item['end_ms']]
    last_time, loaded = -1, []
    for index, frame in enumerate(frames, 1):
        path = Path(frame['file'])
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or not 0 < path.stat().st_size <= 524288:
            fail('SHORT_FRAME_UNAVAILABLE')
        raw = path.read_bytes()
        if (not 0 < len(raw) <= 524288 or len(raw) != frame['size_bytes']
                or hashlib.sha256(raw).hexdigest() != frame['sha256']
                or frame['sequence'] != index or type(frame['timestamp_ms']) is not int
                or not item['start_ms'] <= frame['timestamp_ms'] < item['end_ms']
                or frame['timestamp_ms'] <= last_time
                or (frame['coverage_start_ms'], frame['coverage_end_ms']) != (bounds[index - 1], bounds[index])
                or not item['start_ms'] <= frame['coverage_start_ms'] < frame['coverage_end_ms'] <= item['end_ms']):
            fail('SHORT_FRAME_CHANGED')
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            if image.format != 'JPEG' or (image.width, image.height) != (frame['width'], frame['height']) or max(image.size) > 960:
                fail('SHORT_FRAME_CHANGED')
        last_time = frame['timestamp_ms']
        loaded.append((frame, raw))
    return loaded


def configuration(organization, inputs):
    from visual_drafts.provider import VisualProvider
    config = runner.safe_config(organization)
    if config['model'] != 'gpt-6-luna':
        fail('SHORT_MODEL_CHANGED')
    requests = {}
    for item in inputs:
        provider = VisualProvider(organization)
        for key, value in analysis_options().items():
            setattr(provider, 'protocol' if key == 'visual_protocol' else key, value)
        frames = [{'id': str(uuid.UUID(int=index)), 'sequence': index, 'timestamp_ms': frame['timestamp_ms'],
                   'sha256': frame['sha256'], 'bytes': raw} for index, (frame, raw) in enumerate(checked_input(item), 1)]
        system, schema, request, _ = provider.prepare_request(language='ru', mode='upload', frames=frames)
        requests[item['case_id']] = digest({'system': system, 'schema': schema, 'request': request})
    return {**config, 'prompt_sha256': hashlib.sha256(system.encode()).hexdigest(), 'schema_sha256': digest(schema),
            'analysis_options': analysis_options(), 'canonical_requests': requests,
            'code_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                           for name in CODE_FILES if (ROOT / name).is_file()}}


def experiment_jobs():
    from media_pipeline.models import ProcessingJob
    return list(ProcessingJob.objects.filter(payload__benchmark_experiment=EXPERIMENT))


def refresh_budget(report):
    report['budget'] = runner.ledger([job.id for job in experiment_jobs()])


def guard(report, selected, config, jobs):
    if (report.get('version') != 1 or report.get('experiment') != EXPERIMENT
            or report.get('max_calls') != MAX_CALLS or report.get('configuration') != config):
        fail('SHORT_CONFIGURATION_CHANGED')
    attempted = {job.payload.get('benchmark_case') for job in jobs}
    if (any(case not in CASES for case in selected) or any(case not in CASES for case in attempted)
            or any(case in attempted for case in selected) or len(attempted) != len(jobs)):
        fail('SHORT_CASE_ALREADY_ATTEMPTED')
    if (report['budget']['held_calls'] or any(case.get('status') == 'before_dispatch' for case in report['cases'])
            or any(job.state in {'queued', 'running'} for job in jobs)):
        fail('SHORT_OUTCOME_UNRESOLVED')
    if max(len(jobs), report['budget']['used_or_held_calls']) + len(selected) > MAX_CALLS:
        fail('SHORT_CALL_LIMIT')


def create_case(source, item, run_id):
    from django.db import transaction
    from django.utils import timezone
    from core.models import Organization, WorkingCopy
    from media_pipeline.models import ProcessingJob
    from media_pipeline.worker import LEASE_SECONDS
    from visual_drafts.models import VisualSession, VisualAnalysisWindow
    from visual_drafts.services import persist_frame, safe_frame_path
    loaded = checked_input(item)
    old, member, written = source['old_session'], source['member'], []
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=member.organization_id)
            working = WorkingCopy.objects.select_for_update().get(instruction_id=old.instruction_id)
            if previous.fingerprint_working(old.instruction) != item['working']:
                fail('SHORT_WORKING_CHANGED')
            old.instruction.refresh_from_db()
            old.asset.refresh_from_db()
            if (old.instruction.lifecycle != 'active' or old.instruction.source_asset_id != old.asset_id
                    or old.asset.status != 'ready' or old.asset.sha256 != item.get('asset_sha256', old.asset.sha256)):
                fail('SHORT_SOURCE_CHANGED')
            if ProcessingJob.objects.filter(instruction_id=old.instruction_id, state__in=['queued', 'running']).exists():
                fail('SHORT_JOB_ALREADY_ACTIVE')
            now = timezone.now()
            session = VisualSession.objects.create(organization=member.organization, instruction=old.instruction, asset=old.asset,
                created_by=member.user, mode='upload', state='active', consent_images=True,
                base_working_revision=working.revision, language=working.language, expires_at=now + timedelta(hours=1),
                window_start_ms=item['start_ms'], window_end_ms=item['end_ms'], analysis_scope='window',
                analysis_budget={'max_calls': 1, 'max_cost_microusd': 0, **analysis_options()})
            clones = []
            for frame, raw in loaded:
                clone = persist_frame(session, sequence=frame['sequence'], timestamp_ms=frame['timestamp_ms'],
                    coverage_start_ms=frame['coverage_start_ms'], coverage_end_ms=frame['coverage_end_ms'],
                    content=raw, width=frame['width'], height=frame['height'], idempotency_key=f"fixed-{frame['sequence']}",
                    request_sha256=frame['sha256'])
                written.append(safe_frame_path(clone.storage_key))
                clones.append(clone)
            VisualAnalysisWindow.objects.create(organization=member.organization, session=session, ordinal=1,
                start_ms=item['start_ms'], end_ms=item['end_ms'], frame_ids=[str(frame.id) for frame in clones],
                source_manifest={'sampling': 'verified_short_action_replay', 'input_sha256': item['input_sha256'],
                                 'strategy': item['arm'], 'selection': item['manifest'].get('selection', {})})
            job = ProcessingJob.objects.create(organization=member.organization, instruction=old.instruction,
                asset=old.asset, requested_by=member.user, kind='analyze_video', state='running', stage='starting',
                attempts=1, max_attempts=1, started_at=now, heartbeat_at=now, lease_token=uuid.uuid4(),
                lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
                payload={'visual_session_id': str(session.id), **analysis_options(), 'benchmark_experiment': EXPERIMENT,
                         'benchmark_run_id': run_id, 'benchmark_case': item['case_id']})
            return job, session
    except BaseException:
        for path in written:
            path.unlink(missing_ok=True)
        raise


@contextmanager
def lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open('a') as file:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail('SHORT_ALREADY_RUNNING')
        try:
            yield
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def run_case(source, item, report, persist):
    from media_pipeline.jobs import job_data
    from visual_drafts.models import VisualProposal
    job, session = create_case(source, item, report['run_id'])
    case = {'case_id': item['case_id'], 'clip_id': item['clip_id'], 'arm': item['arm'],
            'job_id': str(job.id), 'session_id': str(session.id), 'instruction_id': str(job.instruction_id),
            'input_sha256': item['input_sha256'], 'working_before': item['working'], 'status': 'before_dispatch'}
    report['cases'].append(case)
    report['status'] = 'running'
    persist()
    print(json.dumps({'case': item['case_id'], 'status': 'started', 'job_id': str(job.id)}), flush=True)
    interrupted = False
    try:
        with previous.capture_validation(case):
            case['wall_ms'] = runner.execute_case(job, item['arm'])
    except BaseException:
        interrupted = True
    finally:
        job.refresh_from_db()
        case['status'], case['job'] = job.state, job_data(job)
        case['sent_frames'] = list(session.frames.order_by('timestamp_ms').values(*previous.FRAME_FIELDS))
        case['identical_prepared_images_and_times'] = (
            [previous.frame_signature(frame) for frame in case['sent_frames']]
            == [previous.frame_signature(frame) for frame in item['frames']])
        case['working_after'] = previous.fingerprint_working(source['old_session'].instruction)
        case['windows'] = list(session.analysis_windows.values('id', 'state', 'error_code'))
        proposal = VisualProposal.objects.filter(source_job_id=job.id).first()
        if proposal:
            case['proposal_id'], case['proposal'] = str(proposal.id), proposal.content
        refresh_budget(report)
        persist()
    receipts = [row for row in report['budget']['reservations'] if str(row['job_id']) == str(job.id)]
    if (interrupted or job.state != 'done' or len(receipts) != 1 or receipts[0]['state'] != 'settled'
            or receipts[0]['unit'] != 'calls' or receipts[0]['max_units'] != 1
            or len(case['windows']) != 1 or case['windows'][0]['state'] != 'completed'
            or not case['identical_prepared_images_and_times'] or case['working_after'] != case['working_before']
            or not proposal or proposal.applied_at or proposal.content.get('schema_version') != '3'):
        fail('SHORT_STOPPED_NO_RETRY')
    print(json.dumps({'case': item['case_id'], 'status': 'done', 'calls': report['budget']['used_or_held_calls']}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--inputs', type=Path)
    parser.add_argument('--source-report', type=Path, default=ROOT / 'artifacts/test-results/real-video-ab-v1.json')
    parser.add_argument('--reference', type=Path, default=ROOT / 'research/real-video-reference.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cases', default=','.join(CASES))
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report, may_save_failure = None, False
    try:
        with lock():
            runner.initialize_django()
            if args.prepare:
                if args.execute or args.inputs:
                    fail('SHORT_PREPARE_ARGUMENTS_INVALID')
                prepare(args.source_report, args.reference, output)
                print(json.dumps({'status': 'prepared', 'model_calls': 0, 'output': str(output)}), flush=True)
                return
            if args.inputs is None:
                fail('SHORT_INPUTS_REQUIRED')
            selected = selected_cases(args.cases)
            bundle = json.loads(args.inputs.read_text())
            if bundle.get('experiment') != EXPERIMENT or bundle.get('model_calls') != 0:
                fail('SHORT_INPUT_BUNDLE_INVALID')
            inputs = bundle['inputs']
            if {item['case_id'] for item in inputs} != set(CASES) or len(inputs) != 4:
                fail('SHORT_INPUT_BUNDLE_INVALID')
            source_report = json.loads(args.source_report.read_text())
            if bundle['source_report_sha256'] != hashlib.sha256(args.source_report.read_bytes()).hexdigest():
                fail('SHORT_SOURCE_REPORT_CHANGED')
            sources = {clip: previous.validate_source(source_report, clip) for clip in CLIPS}
            for item in inputs:
                source = sources[item['clip_id']]
                if (item['instruction_id'] != str(source['old_session'].instruction_id)
                        or item['source_session_id'] != str(source['old_session'].id)
                        or item['asset_sha256'] != source['old_session'].asset.sha256
                        or item['working'] != source['working_before']):
                    fail('SHORT_SOURCE_CHANGED')
            org = sources[CLIPS[0]]['member'].organization
            if any(source['member'].organization_id != org.id for source in sources.values()):
                fail('SHORT_SOURCE_CHANGED')
            config = configuration(org, inputs)
            config['input_bundle_sha256'] = hashlib.sha256(args.inputs.read_bytes()).hexdigest()
            if output.exists():
                report = json.loads(output.read_text())
            else:
                report = {'version': 1, 'experiment': EXPERIMENT, 'run_id': str(uuid.uuid4()), 'max_calls': MAX_CALLS,
                          'configuration': config, 'cases': [], 'automatic_retries': False, 'refinements': False,
                          'paid_api_enabled': False, 'status': 'validating'}
            refresh_budget(report)
            guard(report, selected, config, experiment_jobs())
            run_jobs = {str(job.id) for job in experiment_jobs() if job.payload.get('benchmark_run_id') == report['run_id']}
            if run_jobs != {case['job_id'] for case in report['cases']}:
                fail('SHORT_JOB_CHECKPOINT_MISMATCH')
            runner.quota_quote(org, report['run_id'], len(selected))
            report['status'] = 'ready' if args.execute else 'dry_run_validated'
            previous.atomic_save(output, report)
            may_save_failure = True
            if args.execute:
                for case_id in selected:
                    refresh_budget(report)
                    guard(report, [case_id], configuration_with_bundle(org, inputs, args.inputs), experiment_jobs())
                    item = next(item for item in inputs if item['case_id'] == case_id)
                    run_case(sources[item['clip_id']], item, report, lambda: previous.atomic_save(output, report))
                report['status'] = 'completed'
                previous.atomic_save(output, report)
            print(json.dumps({'status': report['status'], 'calls': report['budget']['used_or_held_calls'], 'output': str(output)}), flush=True)
    except BaseException as error:
        code = str(error) if isinstance(error, runner.BenchmarkFailure) else getattr(error, 'code', 'SHORT_RUN_FAILED')
        if report is not None and may_save_failure:
            report['status'], report['error_code'] = 'halted', code
            previous.atomic_save(output, report)
        print(json.dumps({'status': 'halted', 'error_code': code}), flush=True)
        raise SystemExit(1) from None


def configuration_with_bundle(organization, inputs, path):
    result = configuration(organization, inputs)
    result['input_bundle_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


if __name__ == '__main__':
    main()
