import copy
import uuid

from django.db import transaction
from django.utils import timezone

from core import services as s
from core.models import Organization
from .models import ProcessingJob


def job_data(job):
    result = copy.deepcopy(job.result)
    if job.kind in {'analyze_video', 'analyze_live'} and isinstance(result, dict) and isinstance(result.get('analysis'), dict):
        from visual_drafts.models import VisualSession
        from ai_budget.services import budget_snapshot
        try:
            session_id = uuid.UUID(str(result.get('visual_session_id', '')))
        except (ValueError, TypeError):
            session_id = None
        session = VisualSession.objects.filter(pk=session_id, organization_id=job.organization_id, instruction_id=job.instruction_id).first() if session_id else None
        if session:
            # Receipts can arrive after cancellation or proposal creation. The
            # authoring result is immutable; accounting is a current read model.
            result['analysis']['budget'] = budget_snapshot(session)
    return {
        'id': str(job.id), 'instruction_id': str(job.instruction_id), 'asset_id': str(job.asset_id) if job.asset_id else None,
        'kind': job.kind, 'state': job.state, 'stage': job.stage, 'attempts': job.attempts,
        'error_code': job.error_code or None, 'error_summary': job.error_summary or None,
        'retry_allowed': job.state in ('failed', 'cancelled') and job.retryable and job.attempts < job.max_attempts,
        'manual_allowed': bool(job.asset_id and job.asset.status == 'ready'), 'cancel_requested': job.cancel_requested,
        'result': result, 'created_at': s.utc(job.created_at), 'updated_at': s.utc(job.updated_at),
        'finished_at': s.utc(job.finished_at),
    }


@transaction.atomic
def enqueue_job(member, instruction, kind, payload=None, input_hash=''):
    Organization.objects.select_for_update().get(pk=member.organization_id)
    if instruction.organization_id != member.organization_id:
        s.fail('NOT_FOUND', 'Источник не найден.', 404)
    if kind not in ('prepare_media', 'transcribe', 'draft_steps', 'analyze_video', 'analyze_live'):
        s.fail('VALIDATION_ERROR', 'Неизвестная задача.')
    if kind == 'analyze_live':
        try:
            uuid.UUID(str((payload or {}).get('visual_session_id')))
        except (ValueError, TypeError, AttributeError):
            s.fail('VALIDATION_ERROR', 'Для анализа камеры нужна активная видеосессия.')
        # Camera evidence belongs to the session even when the instruction also has an uploaded source.
        asset = None
    else:
        asset = instruction.source_asset
        if not asset:
            s.fail('NOT_FOUND', 'Источник не найден.', 404)
    if asset and kind != 'prepare_media' and asset.status != 'ready':
        s.fail('MEDIA_NOT_READY', 'Дождитесь подготовки видео.', 409)
    existing = ProcessingJob.objects.filter(instruction=instruction, kind=kind, state__in=['queued', 'running']).first()
    if existing:
        if existing.input_hash != input_hash or existing.payload != (payload or {}):
            s.fail('JOB_ALREADY_RUNNING', 'Для этого источника уже выполняется задача.', 409)
        return existing
    return ProcessingJob.objects.create(organization=member.organization, instruction=instruction,
        asset=asset, requested_by=member.user, kind=kind, payload=payload or {}, input_hash=input_hash)


def job_for(member, job_id, lock=False):
    query = ProcessingJob.objects.select_related('asset', 'instruction').filter(organization=member.organization, instruction__lifecycle='active')
    if lock:
        query = query.select_for_update(of=('self',))
    job = query.filter(pk=job_id).first()
    if not job:
        s.fail('NOT_FOUND', 'Задача не найдена.', 404)
    return job


@transaction.atomic
def cancel_job(member, job_id):
    Organization.objects.select_for_update().get(pk=member.organization_id)
    job = job_for(member, job_id, lock=True)
    if job.state in ('queued', 'running'):
        job.cancel_requested = True
        if job.state == 'queued':
            job.state = 'cancelled'
            job.stage = 'cancelled'
            job.retryable = True
            job.finished_at = timezone.now()
            if job.kind == 'prepare_media' and job.asset.status != 'ready':
                job.asset.status = 'rejected'
                job.asset.save(update_fields=['status', 'updated_at'])
        job.save()
        s.audit(member, 'job.cancel_requested', job)
    return job


@transaction.atomic
def retry_job(member, job_id):
    Organization.objects.select_for_update().get(pk=member.organization_id)
    job = job_for(member, job_id, lock=True)
    if not job_data(job)['retry_allowed']:
        s.fail('RETRY_NOT_ALLOWED', 'Повтор этой задачи недоступен.', 409)
    if ProcessingJob.objects.filter(instruction=job.instruction, kind=job.kind, state__in=['queued', 'running']).exists():
        s.fail('JOB_ALREADY_RUNNING', 'Уже выполняется другая задача.', 409)
    job.state = job.stage = 'queued'
    job.cancel_requested = False
    job.error_code = job.error_summary = ''
    job.lease_token = job.lease_expires_at = job.heartbeat_at = job.finished_at = None
    job.result = {}
    job.save()
    if job.kind == 'prepare_media' and job.asset.status != 'ready':
        job.asset.status = 'quarantined'
        job.asset.save(update_fields=['status', 'updated_at'])
    s.audit(member, 'job.retried', job)
    return job
