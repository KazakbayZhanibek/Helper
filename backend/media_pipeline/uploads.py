import hashlib
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core import services as s
from core.http import DomainError
from core.models import Instruction, MediaAsset, Membership, Organization
from .jobs import enqueue_job, cancel_job
from .models import MediaQuota, UploadSession, ProcessingJob
from .storage import private_path, private_open

MAX_SIZE = 500 * 1024 ** 2


def upload_data(upload):
    return {'id': str(upload.id), 'upload_id': str(upload.id), 'state': upload.state,
            'expires_at': s.utc(upload.expires_at), 'max_size_bytes': MAX_SIZE, 'max_duration_ms': 600000}


def _release(upload, quota):
    if upload.reserved_bytes:
        quota.reserved_bytes = max(0, quota.reserved_bytes - upload.reserved_bytes)
        upload.reserved_bytes = 0
        quota.save(update_fields=['reserved_bytes'])


def expire_uploads(organization):
    """Caller holds the organization lock, which serializes quota accounting."""
    quota, _ = MediaQuota.objects.get_or_create(organization=organization,
        defaults={'limit_bytes': getattr(settings, 'MEDIA_QUOTA_BYTES', 20 * 1024 ** 3)})
    for upload in UploadSession.objects.filter(organization=organization, state__in=['pending', 'uploading'], expires_at__lte=timezone.now()):
        _release(upload, quota)
        upload.state = 'expired'
        upload.error_code = 'UPLOAD_EXPIRED'
        upload.save()
    return quota


@transaction.atomic
def begin_upload(member, instruction_id, data):
    Organization.objects.select_for_update().get(pk=member.organization_id)
    s.allowed_fields(data, {'filename', 'size_bytes', 'language', 'rights_confirmed'})
    filename = data.get('filename')
    size = data.get('size_bytes')
    language = data.get('language', 'ru')
    if not isinstance(filename, str) or not filename.strip() or len(filename) > 255 or filename.rsplit('.', 1)[-1].lower() not in ('mp4', 'mov', 'webm'):
        s.fail('UNSUPPORTED_FILE', 'Выберите файл MP4, MOV или WebM.')
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size > MAX_SIZE:
        s.fail('FILE_TOO_LARGE', 'Размер видео должен быть от 1 байта до 500 МиБ.', 413)
    if language not in ('ru', 'kk', 'mixed') or data.get('rights_confirmed') is not True:
        s.fail('VALIDATION_ERROR', 'Выберите язык и подтвердите право загрузить видео.')
    instruction = s.instruction_for(member, instruction_id, lock=True)
    if instruction.source_asset_id:
        s.fail('SOURCE_ALREADY_EXISTS', 'Источник уже добавлен. Создайте отдельную инструкцию для другого видео.', 409)
    if not instruction.working_copy.is_open or instruction.working_copy.workflow != 'draft':
        s.fail('WORKING_COPY_LOCKED', 'Загрузка доступна в открытом черновике.', 409)
    quota = expire_uploads(member.organization)
    if UploadSession.objects.filter(instruction=instruction, state__in=['pending', 'uploading']).exists():
        s.fail('UPLOAD_ALREADY_EXISTS', 'Загрузка уже начата.', 409)
    if quota.used_bytes + quota.reserved_bytes + size > quota.limit_bytes:
        s.fail('STORAGE_QUOTA_EXCEEDED', 'Недостаточно места для загрузки видео.', 413)
    quota.reserved_bytes += size
    quota.save(update_fields=['reserved_bytes'])
    upload = UploadSession.objects.create(organization=member.organization, instruction=instruction, uploader=member.user,
        filename=filename, language=language, expected_size_bytes=size, reserved_bytes=size,
        expires_at=timezone.now() + timedelta(hours=2))
    s.audit(member, 'upload.started', upload)
    return upload_data(upload), 201


def upload_for(member, upload_id, lock=False):
    query = UploadSession.objects.filter(organization=member.organization, instruction__lifecycle='active')
    if member.role != 'admin':
        query = query.filter(uploader=member.user)
    if lock:
        query = query.select_for_update()
    upload = query.filter(pk=upload_id).first()
    if not upload:
        s.fail('NOT_FOUND', 'Загрузка не найдена.', 404)
    return upload


def completion_data(upload):
    job = ProcessingJob.objects.filter(asset_id=upload.asset_id, kind='prepare_media').order_by('created_at').first()
    return {'upload_id': str(upload.id), 'asset_id': str(upload.asset_id), 'job_id': str(job.id), 'state': 'queued'}


@transaction.atomic
def _mark_receiving(member, upload_id, content_length):
    Organization.objects.select_for_update().get(pk=member.organization_id)
    expire_uploads(member.organization)
    upload = upload_for(member, upload_id, lock=True)
    if upload.state == 'completed':
        return upload, completion_data(upload)
    if upload.state == 'uploading':
        s.fail('UPLOAD_IN_PROGRESS', 'Файл уже принимается. Повторите запрос позже.', 409)
    if upload.state != 'pending':
        s.fail('UPLOAD_CLOSED', 'Сессия загрузки завершена. Начните новую загрузку.', 409)
    if content_length is not None and content_length != upload.expected_size_bytes:
        s.fail('SIZE_MISMATCH', 'Размер файла не совпадает с заявленным.', 400)
    upload.state = 'uploading'
    upload.save(update_fields=['state', 'updated_at'])
    return upload, None


@transaction.atomic
def fail_upload(member, upload_id, code):
    Organization.objects.select_for_update().get(pk=member.organization_id)
    upload = upload_for(member, upload_id, lock=True)
    if upload.state in ('pending', 'uploading'):
        quota = MediaQuota.objects.get(organization=member.organization)
        _release(upload, quota)
        upload.state = 'failed'
        upload.error_code = code
        upload.save()


def receive_upload(member, upload_id, stream, content_length=None):
    try:
        upload, completed = _mark_receiving(member, upload_id, content_length)
    except DomainError as error:
        if error.code == 'SIZE_MISMATCH':
            fail_upload(member, upload_id, error.code)
        raise
    if completed:
        return completed, 202
    key = f'quarantine/{member.organization_id}/{upload.id}/{uuid.uuid4().hex}.original'
    path = private_path(key)
    size, digest, started = 0, hashlib.sha256(), time.monotonic()
    try:
        with private_open(path) as destination:
            while True:
                if time.monotonic() - started > getattr(settings, 'UPLOAD_TIMEOUT_SECONDS', 1800):
                    s.fail('UPLOAD_TIMEOUT', 'Время загрузки истекло.', 408)
                chunk = stream.read(min(1024 * 1024, upload.expected_size_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > upload.expected_size_bytes or size > MAX_SIZE:
                    s.fail('SIZE_MISMATCH', 'Размер файла превышает заявленный.', 413)
                destination.write(chunk)
                digest.update(chunk)
                if not UploadSession.objects.filter(pk=upload.id, state='uploading', expires_at__gt=timezone.now()).exists():
                    s.fail('UPLOAD_CANCELLED', 'Загрузка отменена или истекла.', 409)
        if size != upload.expected_size_bytes:
            s.fail('SIZE_MISMATCH', 'Файл загружен не полностью.', 400)
        with transaction.atomic():
            organization = Organization.objects.select_for_update().get(pk=member.organization_id)
            s.membership(member.user, member.organization_id, s.WRITERS)
            if not Membership.objects.filter(pk=member.pk, is_active=True, user__is_active=True).exists():
                s.fail('AUTH_REQUIRED', 'Доступ пользователя прекращён.', 401)
            upload = upload_for(member, upload_id, lock=True)
            instruction = Instruction.objects.select_for_update().get(pk=upload.instruction_id)
            if upload.state != 'uploading' or upload.expires_at <= timezone.now() or instruction.source_asset_id:
                s.fail('UPLOAD_CLOSED', 'Сессия загрузки больше не активна.', 409)
            if organization.status != 'active':
                s.fail('ORGANIZATION_UNAVAILABLE', 'Организация недоступна.', 403)
            asset = MediaAsset.objects.create(organization=member.organization, storage_key='', original_key=key,
                original_size_bytes=size, sha256=digest.hexdigest(), duration_ms=None, status='quarantined', is_demo=False)
            instruction.source_asset = asset
            instruction.save(update_fields=['source_asset', 'updated_at'])
            s.bump(instruction.working_copy)
            upload.asset = asset
            upload.received_bytes = size
            upload.state = 'completed'
            quota = MediaQuota.objects.get(organization=member.organization)
            _release(upload, quota)
            quota.used_bytes += size
            quota.save(update_fields=['used_bytes'])
            upload.save()
            enqueue_job(member, instruction, 'prepare_media', input_hash=asset.sha256)
            s.audit(member, 'upload.completed', upload)
            return completion_data(upload), 202
    except BaseException as exc:
        try:
            referenced = MediaAsset.objects.filter(original_key=key).exists()
        except Exception:
            referenced = True
        if not referenced:
            path.unlink(missing_ok=True)
        fail_upload(member, upload_id, getattr(exc, 'code', 'UPLOAD_INTERRUPTED'))
        raise


@transaction.atomic
def cancel_upload(member, upload_id):
    Organization.objects.select_for_update().get(pk=member.organization_id)
    upload = upload_for(member, upload_id, lock=True)
    if upload.state == 'completed':
        job = ProcessingJob.objects.filter(asset=upload.asset, kind='prepare_media', state__in=['queued', 'running']).first()
        if job:
            cancel_job(member, job.id)
    elif upload.state in ('pending', 'uploading'):
        _release(upload, MediaQuota.objects.get(organization=member.organization))
        upload.state = 'cancelled'
        upload.save()
    s.audit(member, 'upload.cancelled', upload)
