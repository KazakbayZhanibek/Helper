"""Durable, sequential queue runner. Run one process with SQLite; use PostgreSQL for multiple workers."""
import logging
import os
import signal
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from core.models import MediaAsset, Membership, Organization
from .exceptions import Cancelled, ProcessingFailure
from .models import ProcessingJob
from .telemetry import AttemptMetrics, with_metrics

logger = logging.getLogger(__name__)
LEASE_SECONDS = 90
HEARTBEAT_SECONDS = 15


def _terminate(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)
    except ProcessLookupError:
        pass


def _process_limits():
    # A malicious media file must not fill the disk even before a polling check observes it.
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 ** 3, 1024 ** 3))
    resource.setrlimit(resource.RLIMIT_CPU, (1900, 1905))
    os.umask(0o077)


class JobContext:
    def __init__(self, job):
        self.job_id = job.id
        self.lease_token = job.lease_token
        self.last_heartbeat = 0.0
        self.metrics = AttemptMetrics.for_job(job) if job.kind in ('analyze_video', 'analyze_live') else None

    def _assert_current(self, job):
        if (job.state != 'running' or job.lease_token != self.lease_token or job.cancel_requested
                or not job.lease_expires_at or job.lease_expires_at <= timezone.now()
                or job.organization.status != 'active'
                or job.instruction.lifecycle != 'active'
                or (job.asset_id and job.asset.status == 'deleted')
                or not Membership.objects.filter(organization_id=job.organization_id, user_id=job.requested_by_id,
                    is_active=True, user__is_active=True, role__in=['admin', 'author', 'reviewer']).exists()):
            raise Cancelled()

    def check(self):
        with transaction.atomic():
            job = ProcessingJob.objects.select_for_update(of=('self',)).select_related('organization', 'instruction', 'asset').get(pk=self.job_id)
            self._assert_current(job)
            if time.monotonic() - self.last_heartbeat >= HEARTBEAT_SECONDS:
                now = timezone.now()
                job.heartbeat_at, job.lease_expires_at = now, now + timedelta(seconds=LEASE_SECONDS)
                job.save(update_fields=['heartbeat_at', 'lease_expires_at', 'updated_at'])
                self.last_heartbeat = time.monotonic()

    def commit_guard(self):
        """Call inside transaction.atomic(); the row lock fences stale/cancelled attempts until commit."""
        job = ProcessingJob.objects.select_for_update(of=('self',)).select_related('organization', 'instruction', 'asset').get(pk=self.job_id)
        self._assert_current(job)
        return job

    def stage(self, label):
        self.check()
        ProcessingJob.objects.filter(pk=self.job_id, lease_token=self.lease_token, state='running').update(stage=label[:64], updated_at=timezone.now())

    def run(self, argv, timeout, *, input_bytes=None, cwd=None, env=None):
        """Run an argument array without a shell, with cancellation, heartbeat and bounded logs."""
        if not isinstance(argv, (tuple, list)) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise ValueError('Commands must be argument arrays')
        if input_bytes is not None and (not isinstance(input_bytes, bytes) or len(input_bytes) > 262144):
            raise ValueError('Process input must be bounded bytes')
        self.check()
        started = time.monotonic()
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr, tempfile.TemporaryFile() as stdin:
            if input_bytes is not None:
                stdin.write(input_bytes)
                stdin.seek(0)
            try:
                process = subprocess.Popen(argv, stdin=stdin if input_bytes is not None else subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    cwd=cwd, env=env, shell=False, start_new_session=True, preexec_fn=_process_limits if os.name == 'posix' else None)
            except (OSError, ValueError):
                raise ProcessingFailure('PROCESS_RUNTIME_UNAVAILABLE', 'Не удалось запустить обработчик на сервере.', retryable=True)
            try:
                while process.poll() is None:
                    self.check()
                    if time.monotonic() - started > timeout:
                        raise ProcessingFailure('PROCESS_TIMEOUT', 'Обработка заняла слишком много времени.', retryable=True)
                    if os.fstat(stdout.fileno()).st_size > 4 * 1024 ** 2 or os.fstat(stderr.fileno()).st_size > 2 * 1024 ** 2:
                        raise ProcessingFailure('PROCESS_OUTPUT_LIMIT', 'Обработчик превысил допустимый объём вывода.')
                    time.sleep(0.2)
                self.check()
                if os.fstat(stdout.fileno()).st_size > 4 * 1024 ** 2 or os.fstat(stderr.fileno()).st_size > 2 * 1024 ** 2:
                    raise ProcessingFailure('PROCESS_OUTPUT_LIMIT', 'Обработчик превысил допустимый объём вывода.')
                stdout.seek(0)
                stderr.seek(0)
                out = stdout.read(4 * 1024 ** 2).decode('utf-8', errors='replace')
                err = stderr.read(2 * 1024 ** 2).decode('utf-8', errors='replace')
                if process.returncode:
                    # Raw codec/provider output may contain filenames or source text: keep it out of API/logs.
                    raise ProcessingFailure('PROCESS_FAILED', 'Обработчик не смог прочитать или подготовить файл.', retryable=False)
                return subprocess.CompletedProcess(argv, process.returncode, out, err)
            finally:
                _terminate(process)

    def call(self, function, timeout):
        """Heartbeat around a bounded provider call.

        Authoring writes stay on this thread behind commit_guard. Provider usage
        receipts may settle independently on their own connection: cancellation
        cannot undo an already dispatched external request or its cost.
        """
        self.check()
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(function)
        started = time.monotonic()
        try:
            while True:
                self.check()
                if time.monotonic() - started > timeout:
                    raise ProcessingFailure('PROVIDER_TIMEOUT', 'Сервис ИИ не ответил вовремя.', retryable=True)
                try:
                    result = future.result(timeout=0.5)
                    self.check()
                    return result
                except FutureTimeout:
                    continue
        finally:
            future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)


@transaction.atomic
def recover_expired_jobs():
    for job in ProcessingJob.objects.select_for_update().filter(state='running', lease_expires_at__lte=timezone.now()):
        job.lease_token = job.lease_expires_at = None
        if job.cancel_requested:
            job.state, job.stage, job.retryable = 'cancelled', 'cancelled', True
            job.finished_at = timezone.now()
        elif job.attempts < job.max_attempts:
            job.state = job.stage = 'queued'
        else:
            job.state, job.stage = 'failed', 'failed'
            job.error_code, job.error_summary = 'WORKER_INTERRUPTED', 'Обработка была прервана несколько раз.'
            job.retryable = False
            job.finished_at = timezone.now()
        job.save()
        if job.kind == 'prepare_media' and job.state in ('failed', 'cancelled'):
            MediaAsset.objects.filter(pk=job.asset_id).exclude(status='ready').update(status='rejected')


@transaction.atomic
def claim_job():
    # Compare-and-set complements the PostgreSQL row lock and prevents a lost claim.
    candidates = ProcessingJob.objects.select_for_update().filter(state='queued', cancel_requested=False,
        organization__status='active', instruction__lifecycle='active').order_by('created_at')
    for job in candidates[:20]:
        now, token = timezone.now(), uuid.uuid4()
        changed = ProcessingJob.objects.filter(pk=job.id, state='queued', cancel_requested=False).update(
            state='running', stage='starting', attempts=F('attempts') + 1, lease_token=token,
            lease_expires_at=now + timedelta(seconds=LEASE_SECONDS), heartbeat_at=now, started_at=now,
            finished_at=None, updated_at=now)
        if changed:
            return ProcessingJob.objects.select_related('asset', 'instruction', 'organization', 'requested_by').get(pk=job.id)
    return None


def _finish(job, state, result=None, error=None, *, context=None):
    with transaction.atomic():
        locked = ProcessingJob.objects.select_for_update().get(pk=job.id)
        if locked.lease_token != job.lease_token or locked.state != 'running':
            return
        if locked.cancel_requested:
            state, error = 'cancelled', None
        locked.state = locked.stage = state
        # A cancelled/failed long analysis still has durable per-window progress.
        # Preserve that checkpoint rather than replacing it with an empty result.
        if result is not None:
            locked.result = result
        if context is not None:
            locked.result = with_metrics(locked.result, context, outcome=state)
        locked.error_code = error.code if error else ''
        locked.error_summary = str(error.message)[:500] if error else ''
        locked.retryable = (error.retryable if error else state == 'cancelled')
        locked.lease_expires_at = None
        locked.lease_token = None
        locked.finished_at = timezone.now()
        locked.save()
        if locked.kind == 'prepare_media' and state in ('failed', 'cancelled'):
            MediaAsset.objects.filter(pk=locked.asset_id).exclude(status='ready').update(status='rejected')


def process_one():
    recover_expired_jobs()
    job = claim_job()
    if job is None:
        return False
    context = JobContext(job)
    try:
        context.check()
        if job.kind == 'prepare_media':
            from .media import prepare_media
            if job.asset.status == 'ready' and job.asset.storage_key:
                result = {'asset_id': str(job.asset_id), 'duration_ms': job.asset.duration_ms, 'has_audio': job.asset.has_audio, 'manual_allowed': True}
            else:
                result = prepare_media(job, context)
        elif job.kind in ('analyze_video', 'analyze_live'):
            from visual_drafts.worker import process_job
            result = process_job(job, context)
        else:
            from ai_drafts.worker import process_job
            result = process_job(job, context)
        context.check()
        _finish(job, 'done', result=result, context=context)
    except Cancelled:
        _finish(job, 'cancelled', context=context)
    except ProcessingFailure as exc:
        _finish(job, 'failed', error=exc, context=context)
    except Exception:
        # Log identifiers only: provider exceptions can include private transcripts or keys.
        logger.error('Unexpected processing failure: job=%s kind=%s', job.id, job.kind)
        _finish(job, 'failed', error=ProcessingFailure('INTERNAL_PROCESSING_ERROR', 'Ошибка обработки. Можно повторить задачу.', retryable=True), context=context)
    return True
