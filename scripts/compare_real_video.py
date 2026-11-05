"""Bounded real-model comparison on licensed playbacks; never applies proposals.

Dry run (default) validates inputs, configuration and quota without model calls.
--execute uses the application's database and budget ledger. Each source is a
separate reviewable draft; each strategy has a one-call session, with no retries.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
MAX_CALLS = 8
MAX_CLIPS = 4
MAX_CLIP_BYTES = 150 * 1024 * 1024
STRATEGIES = ('uniform_legacy', 'events')


class BenchmarkFailure(Exception):
    """Only stable, non-secret failure codes leave the runner boundary."""


@dataclass(frozen=True)
class PreparedClip:
    id: str
    path: Path
    sha256: str
    metadata: dict
    provenance: dict
    size_bytes: int


def manifest_entries(path, max_calls):
    if type(max_calls) is not int or not 2 <= max_calls <= MAX_CALLS:
        raise BenchmarkFailure('BENCHMARK_CALL_LIMIT_INVALID')
    try:
        data = json.loads(path.read_text())
        entries = data['clips']
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_CLIPS or 2 * len(entries) > max_calls:
            raise ValueError()
        ids, source_urls = set(), set()
        result = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get('rights_confirmed') is not True:
                raise ValueError()
            clip_id = entry['id']
            if not isinstance(clip_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', clip_id) or clip_id in ids:
                raise ValueError()
            for key in ('source_url', 'license_url'):
                url = urlsplit(entry[key])
                if url.scheme != 'https' or not url.hostname or url.username or url.password:
                    raise ValueError()
            if entry['source_url'] in source_urls:
                raise ValueError()
            value = dict(entry)
            value['path'] = (path.parent / entry['path']).resolve()
            if not value['path'].is_file() or not 0 < value['path'].stat().st_size <= MAX_CLIP_BYTES:
                raise ValueError()
            ids.add(clip_id)
            source_urls.add(entry['source_url'])
            result.append(value)
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise BenchmarkFailure('BENCHMARK_MANIFEST_INVALID') from None


def initialize_django():
    sys.path.insert(0, str(ROOT / 'scripts'))
    from dev import load_environment
    os.environ.update(load_environment())
    sys.path.insert(0, str(ROOT / 'backend'))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()


def prepare_inputs(entries, directory):
    from media_pipeline.media import binary, local_input, validate_probe
    result, hashes = [], set()
    for entry in entries:
        path = directory / f'{entry["id"]}.mp4'
        shutil.copyfile(entry['path'], path)
        path.chmod(0o600)
        if not 0 < path.stat().st_size <= MAX_CLIP_BYTES:
            raise BenchmarkFailure('BENCHMARK_MEDIA_SIZE_INVALID')
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        if digest in hashes:
            raise BenchmarkFailure('BENCHMARK_DUPLICATE_SOURCE')
        hashes.add(digest)
        try:
            value = subprocess.run([binary('ffprobe'), '-v', 'error', *local_input(path),
                                    '-show_streams', '-show_format', '-of', 'json'],
                                   check=True, capture_output=True, text=True, timeout=30)
            if len(value.stdout) > 1024 * 1024:
                raise ValueError()
            data = json.loads(value.stdout)
            metadata = validate_probe(data)
            if metadata['duration_ms'] > 30000 or max(metadata['width'], metadata['height']) > 1280:
                raise ValueError()
        except Exception:
            raise BenchmarkFailure('BENCHMARK_PREPARED_MEDIA_INVALID') from None
        provenance = {key: entry[key] for key in ('source_url', 'license_url', 'rights_confirmed', 'author', 'category', 'note') if key in entry}
        result.append(PreparedClip(entry['id'], path, digest, metadata, provenance, path.stat().st_size))
    return result


def safe_config(organization):
    from visual_drafts.provider import capabilities, SYSTEM
    config = capabilities(organization)
    if not config['available'] or config['provider'] != 'codex':
        raise BenchmarkFailure('BENCHMARK_CODEX_REQUIRED')
    if config['budget']['mode'] != 'subscription_calls':
        raise BenchmarkFailure('BENCHMARK_CASH_PROVIDER_REFUSED')
    return {'provider': config['provider'], 'model': config['model'], 'mode': 'subscription_calls',
            'prompt_sha256': hashlib.sha256(SYSTEM.encode()).hexdigest(),
            'schema_sha256': hashlib.sha256((ROOT / 'contracts/visual-step-draft.schema.json').read_bytes()).hexdigest()}


def quota_quote(organization, run_id, planned_calls):
    from ai_budget.services import preflight
    from visual_drafts.provider import VisualProvider
    session = SimpleNamespace(id=run_id, organization_id=organization.id, organization=organization,
                              analysis_budget={'max_calls': planned_calls, 'max_cost_microusd': 0})
    preflight(session, planned_calls, provider=VisualProvider(organization))


def resolve_member(organization_id, email):
    from core.models import Membership
    member = Membership.objects.select_related('organization', 'user').filter(
        organization_id=organization_id, organization__status='active', user__email=email,
        user__is_active=True, is_active=True, role__in=['admin', 'author', 'reviewer']).first()
    if not member:
        raise BenchmarkFailure('BENCHMARK_MEMBER_UNAVAILABLE')
    return member


def create_source(member, clip, run_id):
    from django.db import transaction
    from core.models import Instruction, MediaAsset, Organization, WorkingCopy
    from media_pipeline.models import MediaQuota
    from media_pipeline.storage import private_path, private_open
    key = f'benchmark/{member.organization_id}/{run_id}/{uuid.uuid4()}.mp4'
    target = private_path(key)
    with clip.path.open('rb') as source, private_open(target) as dest:
        shutil.copyfileobj(source, dest)
    with target.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != clip.sha256:
            target.unlink(missing_ok=True)
            raise BenchmarkFailure('BENCHMARK_COPY_CHANGED')
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=member.organization_id)
            quota, _ = MediaQuota.objects.get_or_create(organization=member.organization)
            if quota.used_bytes + quota.reserved_bytes + clip.size_bytes > quota.limit_bytes:
                raise BenchmarkFailure('BENCHMARK_STORAGE_LIMIT')
            quota.used_bytes += clip.size_bytes
            quota.save(update_fields=['used_bytes'])
            asset = MediaAsset.objects.create(organization=member.organization, storage_key=key, sha256=clip.sha256,
                                             original_size_bytes=clip.size_bytes, is_demo=False, status='ready', **clip.metadata)
            instruction = Instruction.objects.create(organization=member.organization, owner=member.user, source_asset=asset)
            WorkingCopy.objects.create(organization=member.organization, instruction=instruction, language='ru',
                                       title=f'A/B видео: {clip.id}',
                                       purpose='Сравнение выборки кадров на разрешённом публичном видео. Предложения не применяются и не публикуются.')
            return instruction
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def create_case(member, instruction, strategy):
    from django.db import transaction
    from django.utils import timezone
    from core.models import Organization
    from media_pipeline.models import ProcessingJob
    from media_pipeline.worker import LEASE_SECONDS
    from visual_drafts.models import VisualSession, VisualAnalysisWindow
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=member.organization_id)
        now = timezone.now()
        session = VisualSession.objects.create(organization=member.organization, instruction=instruction,
            asset=instruction.source_asset, created_by=member.user, mode='upload', state='active', consent_images=True,
            base_working_revision=1, language='ru', expires_at=now + timedelta(hours=1),
            window_start_ms=0, window_end_ms=instruction.source_asset.duration_ms, analysis_scope='window',
            analysis_budget={'max_calls': 1, 'max_cost_microusd': 0})
        VisualAnalysisWindow.objects.create(organization=member.organization, session=session, ordinal=1,
                                           start_ms=0, end_ms=instruction.source_asset.duration_ms)
        # Never expose a queued job to the separate development worker. This
        # process owns its lease and executes the normal guarded pipeline once.
        job = ProcessingJob.objects.create(organization=member.organization, instruction=instruction,
            asset=instruction.source_asset, requested_by=member.user, kind='analyze_video', state='running',
            stage='starting', attempts=1, max_attempts=1, started_at=now, heartbeat_at=now,
            lease_token=uuid.uuid4(), lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
            payload={'visual_session_id': str(session.id), 'benchmark_strategy': strategy})
        return job, session


def ledger(job_ids):
    from ai_budget.models import AIUsageReservation
    rows = list(AIUsageReservation.objects.filter(job_id__in=job_ids).exclude(state='released').order_by('created_at').values(
        'id', 'job_id', 'session_id', 'provider', 'model', 'unit', 'max_units', 'actual_units', 'state',
        'dispatched_at', 'settled_at', 'usage'))
    return {'used_or_held_calls': len(rows), 'settled_calls': sum(row['actual_units'] or 0 for row in rows if row['state'] == 'settled'),
            'held_calls': sum(row['max_units'] for row in rows if row['state'] != 'settled'),
            'reservations': rows, 'usd_cost': None, 'currency': None}


def execute_case(job, strategy):
    from django.test.utils import override_settings
    from media_pipeline.exceptions import Cancelled, ProcessingFailure
    from media_pipeline.worker import JobContext, _finish
    from visual_drafts.worker import process_job
    context = JobContext(job)
    started = time.monotonic()
    try:
        with override_settings(VIDEO_FRAME_STRATEGY=strategy):
            context.check()
            result = process_job(job, context)
            context.check()
        _finish(job, 'done', result=result, context=context)
    except Cancelled:
        _finish(job, 'cancelled', context=context)
    except ProcessingFailure as error:
        _finish(job, 'failed', error=error, context=context)
    except BaseException:
        _finish(job, 'failed', error=ProcessingFailure('BENCHMARK_INTERRUPTED', 'Проверка остановлена; автоматического повтора нет.'), context=context)
        raise
    job.refresh_from_db()
    return round((time.monotonic() - started) * 1000, 3)


def execute_comparison(member, clips, report, persist, *, max_calls):
    from media_pipeline.jobs import job_data
    from visual_drafts.models import VisualProposal
    job_ids = []
    for clip_number, clip in enumerate(clips):
        if safe_config(member.organization) != report['configuration']:
            raise BenchmarkFailure('BENCHMARK_CONFIGURATION_CHANGED')
        instruction = create_source(member, clip, report['run_id'])
        report['sources'].append({'clip_id': clip.id, 'sha256': clip.sha256, **clip.metadata,
                                  'provenance': clip.provenance, 'instruction_id': str(instruction.id),
                                  'url': f'http://127.0.0.1:4173/o/{member.organization_id}/instructions/{instruction.id}/edit'})
        persist()
        order = STRATEGIES if clip_number % 2 == 0 else tuple(reversed(STRATEGIES))
        for strategy in order:
            if len(report['cases']) >= max_calls or ledger(job_ids)['used_or_held_calls'] >= max_calls:
                raise BenchmarkFailure('BENCHMARK_CALL_LIMIT')
            if safe_config(member.organization) != report['configuration']:
                raise BenchmarkFailure('BENCHMARK_CONFIGURATION_CHANGED')
            job, session = create_case(member, instruction, strategy)
            job_ids.append(job.id)
            case = {'clip_id': clip.id, 'strategy': strategy, 'job_id': str(job.id), 'session_id': str(session.id),
                    'status': 'before_dispatch', 'max_calls': 1}
            report['cases'].append(case)
            persist()  # Durable record exists before any provider dispatch.
            print(json.dumps({'clip': clip.id, 'strategy': strategy, 'status': 'started', 'job': str(job.id)}), flush=True)
            failure = None
            try:
                case['wall_ms'] = execute_case(job, strategy)
            except BaseException as error:
                failure = error
            finally:
                job.refresh_from_db()
                case['job'] = job_data(job)
                case['windows'] = list(session.analysis_windows.values('id', 'state', 'error_code', 'source_manifest', 'content'))
                case['sent_frames'] = list(session.frames.order_by('timestamp_ms').values(
                    'id', 'timestamp_ms', 'width', 'height', 'size_bytes', 'sha256'))
                proposal = VisualProposal.objects.filter(source_job_id=job.id).first()
                if proposal:
                    case['proposal_id'], case['proposal'] = str(proposal.id), proposal.content
                report['budget'] = ledger(job_ids)
                case['status'] = job.state
                persist()
            case_rows = [row for row in report['budget']['reservations'] if row['job_id'] == job.id]
            if failure is not None:
                raise BenchmarkFailure('BENCHMARK_INTERRUPTED_NO_RETRY') from None
            if (job.state != 'done' or len(case_rows) != 1 or case_rows[0]['state'] != 'settled'
                    or case_rows[0]['unit'] != 'calls' or case_rows[0]['max_units'] != 1
                    or session.analysis_windows.count() != 1
                    or not all(window['state'] == 'completed' for window in case['windows'])
                    or report['budget']['used_or_held_calls'] > max_calls):
                raise BenchmarkFailure('BENCHMARK_CASE_FAILED_NO_RETRY')
            working = instruction.working_copy
            if working.revision != 1 or working.steps.exists() or instruction.current_version_id or (proposal and proposal.applied_at):
                raise BenchmarkFailure('BENCHMARK_DRAFT_MUTATED')
            print(json.dumps({'clip': clip.id, 'strategy': strategy, 'status': job.state,
                              'wall_ms': case['wall_ms'], 'calls': report['budget']['used_or_held_calls']}), flush=True)
    report['status'] = 'completed'
    report['working_copies_unchanged'] = True
    persist()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-calls', type=int, default=8)
    parser.add_argument('--organization', default='7ea2d071-f15f-4fe9-b440-f1622b3e17cf')
    parser.add_argument('--user-email', default='admin@demo.local')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'version': 1, 'run_id': str(uuid.uuid4()), 'status': 'validating',
              'max_calls': args.max_calls, 'refinements': False, 'automatic_retries': False,
              'paid_api_enabled': False, 'sources': [], 'cases': [],
              'budget': {'used_or_held_calls': 0, 'settled_calls': 0, 'held_calls': 0, 'usd_cost': None, 'currency': None},
              'limitations': ['One model response per clip and strategy; stochastic variation remains.',
                              'Timing excludes upload and media normalization.',
                              'Subscription headroom is not available from the local ledger; quota errors stop the run.']}
    # Exclusive output ownership prevents accidentally rerunning a partial test.
    try:
        with output.open('x', encoding='utf-8') as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
    except FileExistsError:
        raise SystemExit('BENCHMARK_OUTPUT_EXISTS: choose a new report only after inspecting the previous run.')
    def persist():
        temporary = output.with_name(output.name + '.tmp')
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    try:
        entries = manifest_entries(args.manifest.resolve(), args.max_calls)
        initialize_django()
        member = resolve_member(args.organization, args.user_email)
        report['configuration'] = safe_config(member.organization)
        report['planned_calls'] = 2 * len(entries)
        quota_quote(member.organization, report['run_id'], report['planned_calls'])
        with tempfile.TemporaryDirectory(prefix='real-video-ab-') as directory:
            clips = prepare_inputs(entries, Path(directory))
            report['validated_inputs'] = [{'clip_id': clip.id, 'sha256': clip.sha256, 'size_bytes': clip.size_bytes,
                                            **clip.metadata, 'provenance': clip.provenance} for clip in clips]
            report['status'] = 'ready' if args.execute else 'dry_run_validated'
            persist()
            if args.execute:
                execute_comparison(member, clips, report, persist, max_calls=args.max_calls)
        print(json.dumps({'status': report['status'], 'calls': report['budget']['used_or_held_calls'], 'output': str(output)}), flush=True)
    except BaseException as error:
        report['status'] = 'halted'
        report['error_code'] = str(error) if isinstance(error, BenchmarkFailure) else getattr(error, 'code', 'BENCHMARK_FAILED')
        persist()
        print(json.dumps({'status': 'halted', 'error_code': report['error_code'], 'calls': report['budget']['used_or_held_calls']}), flush=True)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
