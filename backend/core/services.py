"""Transactional manual workflow. Views never accept trusted role/state metadata."""
import copy
import hashlib
import json
import unicodedata
import uuid
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.db.models import F, Max
from django.utils import timezone
from .http import DomainError
from .models import Organization, Membership, MediaAsset, Instruction, WorkingCopy, WorkingStep, PublishedVersion, AuditEvent, IdempotencyRecord

WRITERS = {'admin', 'author', 'reviewer'}
REVIEWERS = {'admin', 'reviewer'}
CONFIRMATIONS = {'order', 'terminology', 'completeness', 'relevance', 'privacy'}


def fail(code, message, status=400, fields=None, details=None):
    raise DomainError(code, message, status, fields, details)


def membership(user, organization_id, roles=None):
    member = Membership.objects.select_related('organization').filter(organization_id=organization_id, user=user, is_active=True).first()
    if not member:
        fail('NOT_FOUND', 'Организация не найдена.', 404)
    if member.organization.status != 'active':
        fail('ORGANIZATION_SUSPENDED', 'Доступ к организации приостановлен.', 403)
    if roles and member.role not in roles:
        fail('FORBIDDEN', 'Недостаточно прав для этого действия.', 403)
    return member


def instruction_for(member, instruction_id, lock=False):
    query = Instruction.objects.select_related('source_asset', 'owner', 'current_version').filter(organization=member.organization, lifecycle='active')
    if lock:
        # Do not lock nullable joined rows on PostgreSQL.
        query = query.select_for_update(of=('self',))
    instruction = query.filter(id=instruction_id).first()
    if not instruction or (member.role == 'reader' and not instruction.current_version_id):
        fail('NOT_FOUND', 'Инструкция не найдена.', 404)
    return instruction


def utc(value):
    return value.isoformat().replace('+00:00', 'Z') if value else None


def etag(working):
    return f'"wc-{working.id}-r{working.revision}"'


def source_data(asset):
    if not asset:
        return None
    base = f'/api/v1/orgs/{asset.organization_id}/assets/{asset.id}'
    return {'id': str(asset.id), 'duration_ms': asset.duration_ms, 'status': asset.status, 'is_demo': asset.is_demo, 'has_audio': asset.has_audio, 'playback_url': f'{base}/playback' if asset.status == 'ready' else None, 'thumbnail_url': f'{base}/thumbnail' if asset.status == 'ready' and asset.thumbnail_key else None}


def step_evidence_frames(step):
    """Resolve only this step's trusted image links; never infer phases or new links."""
    if not step.evidence_visual_session_id or not isinstance(step.evidence_frame_ids, list):
        return []
    refs = set()
    for value in step.evidence_frame_ids:
        if not isinstance(value, str):
            continue
        try:
            refs.add(uuid.UUID(value))
        except ValueError:
            continue
    if not refs:
        return []
    # Import locally: the visual service uses the core authoring workflow too.
    from visual_drafts.models import FrameEvidence
    frames = FrameEvidence.objects.filter(
        id__in=refs,
        organization_id=step.organization_id,
        session_id=step.evidence_visual_session_id,
        session__organization_id=step.organization_id,
        session__instruction__organization_id=step.organization_id,
        session__instruction__working_copy__id=step.working_copy_id,
        session__instruction__lifecycle='active',
        session__asset_id=F('session__instruction__source_asset_id'),
        session__mode='upload',
        session__frames_purged_at__isnull=True,
    ).order_by('timestamp_ms', 'sequence', 'id')[:12]
    return [
        {'id': str(frame.id), 'sequence': frame.sequence, 'timestamp_ms': frame.timestamp_ms,
         'width': frame.width, 'height': frame.height,
         'url': f'/api/v1/orgs/{step.organization_id}/visual-frames/{frame.id}'}
        for frame in frames
    ]


def step_data(step):
    return {'id': str(step.id), 'ordinal': step.ordinal, 'title': step.title, 'description': step.description, 'start_ms': step.start_ms, 'end_ms': step.end_ms, 'origin': step.origin, 'checked': step.checked_by_id is not None, 'checked_by': str(step.checked_by_id) if step.checked_by_id else None, 'evidence_segment_ids': step.evidence_segment_ids, 'evidence_transcript_revision': step.evidence_transcript_revision, 'evidence_frame_ids': step.evidence_frame_ids, 'evidence_visual_session_id': str(step.evidence_visual_session_id) if step.evidence_visual_session_id else None, 'evidence_frames': step_evidence_frames(step), 'warning_codes': step.warning_codes}


def working_data(working):
    return {'id': str(working.id), 'instruction_id': str(working.instruction_id), 'revision': working.revision, 'etag': etag(working), 'editable': working.is_open and working.workflow == 'draft', 'is_open': working.is_open, 'workflow': working.workflow, 'title': working.title, 'purpose': working.purpose, 'language': working.language, 'steps': [step_data(s) for s in working.steps.all()], 'source': source_data(working.instruction.source_asset), 'return_reason': working.return_reason}


def version_data(version):
    if version is None:
        return None
    return {'id': str(version.id), 'version_number': version.version_number, 'published_at': utc(version.published_at), 'published_by': version.published_by.display_name, 'snapshot': copy.deepcopy(version.snapshot)}


def summary(instruction, reader=False):
    working = instruction.working_copy
    version = instruction.current_version
    use_publication = reader or not working.is_open
    content = version.snapshot if use_publication and version else {'title': working.title, 'purpose': working.purpose, 'language': working.language}
    status = working.workflow if working.is_open else ('published' if version else 'draft')
    source = source_data(instruction.source_asset)
    return {'id': str(instruction.id), 'title': content['title'], 'purpose': content['purpose'], 'language': content['language'], 'status': 'published' if reader else status, 'step_count': len(version.snapshot['steps']) if use_publication and version else working.steps.count(), 'duration_ms': instruction.source_asset.duration_ms if instruction.source_asset else None, 'source_is_demo': source['is_demo'] if source else False, 'thumbnail_url': source['thumbnail_url'] if source else None, 'current_version_id': str(version.id) if version else None, 'version_number': version.version_number if version else None, 'updated_at': utc(instruction.updated_at), 'owner_name': instruction.owner.display_name}


def require_match(working, supplied):
    if not supplied:
        fail('PRECONDITION_REQUIRED', 'Для изменения нужна текущая версия инструкции.', 428)
    if supplied != etag(working):
        fail('REVISION_CONFLICT', 'Инструкцию изменили в другом окне. Загрузите актуальную версию.', 412, details={'actual_revision': working.revision})


def ensure_dict(data):
    if not isinstance(data, dict):
        fail('VALIDATION_ERROR', 'Ожидается объект JSON.')


def allowed_fields(data, allowed):
    ensure_dict(data)
    unknown = set(data) - set(allowed)
    if unknown:
        fail('VALIDATION_ERROR', 'Запрос содержит недопустимые поля.', fields={k: ['Поле нельзя изменять.'] for k in sorted(unknown)})


def text_field(data, name, minimum, maximum):
    raw = data.get(name)
    if not isinstance(raw, str):
        fail('VALIDATION_ERROR', 'Проверьте обязательные поля.', fields={name: ['Введите текст.']})
    value = unicodedata.normalize('NFC', raw).strip()
    if not minimum <= len(value) <= maximum:
        fail('VALIDATION_ERROR', 'Проверьте длину текста.', fields={name: [f'От {minimum} до {maximum} символов.']})
    return value


def content_fields(data):
    title = text_field(data, 'title', 3, 120)
    purpose = text_field(data, 'purpose', 10, 600)
    language = data.get('language')
    if not isinstance(language, str) or language not in {'ru', 'kk', 'mixed'}:
        fail('VALIDATION_ERROR', 'Выберите язык инструкции.', fields={'language': ['Выберите русский, казахский или смешанный.']})
    return title, purpose, language


def audit(member, action, obj):
    AuditEvent.objects.create(organization=member.organization, actor=member.user, action=action, object_id=obj.id)


def bump(working):
    working.revision += 1
    working.save()
    Instruction.objects.filter(pk=working.instruction_id).update(updated_at=timezone.now())


def idempotent(request, member, operation):
    key = request.headers.get('Idempotency-Key')
    if not key or len(key) > 128:
        fail('VALIDATION_ERROR', 'Нужен Idempotency-Key длиной до 128 символов.')
    ensure_dict(request.data)
    digest = hashlib.sha256(json.dumps(request.data, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    # Serialize changes to this org. This also protects creation of idempotency records.
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=member.organization_id)
        scope = dict(organization=member.organization, actor=member.user, route=request.path, key=key)
        previous = IdempotencyRecord.objects.filter(**scope).first()
        if previous and previous.created_at < timezone.now() - timedelta(hours=24):
            previous.delete()
            previous = None
        if previous:
            if previous.request_hash != digest:
                fail('IDEMPOTENCY_KEY_REUSED', 'Этот ключ уже использован для другого запроса.', 409)
            return previous.response_json, previous.response_status
        payload, status = operation()
        IdempotencyRecord.objects.create(**scope, request_hash=digest, response_json=payload, response_status=status)
        return payload, status


def create_instruction(member, data):
    allowed_fields(data, {'title', 'purpose', 'language', 'demo_source'})
    title, purpose, language = content_fields(data)
    if 'demo_source' in data and type(data['demo_source']) is not bool:
        fail('VALIDATION_ERROR', 'demo_source должен быть логическим значением.')
    source = None
    if data.get('demo_source'):
        if not settings.DEMO_MODE:
            fail('FORBIDDEN', 'Демонстрационные материалы отключены.', 403)
        demo_path = settings.PRIVATE_MEDIA_ROOT / 'demo-process.mp4'
        if not demo_path.is_file():
            fail('DEPENDENCY_UNAVAILABLE', 'Демонстрационное видео ещё не подготовлено.', 503)
        source = MediaAsset.objects.create(organization=member.organization, storage_key='demo-process.mp4', duration_ms=settings.DEMO_DURATION_MS)
    instruction = Instruction.objects.create(organization=member.organization, owner=member.user, source_asset=source)
    working = WorkingCopy.objects.create(organization=member.organization, instruction=instruction, title=title, purpose=purpose, language=language)
    audit(member, 'instruction.created', instruction)
    return working_data(working), 201


def validate_steps(data, working):
    raw_steps = data.get('steps')
    if not isinstance(raw_steps, list) or len(raw_steps) > 30:
        fail('VALIDATION_ERROR', 'Допустимо не больше 30 шагов.', fields={'steps': ['Нужен список из 0–30 шагов.']})
    existing = {str(s.id): s for s in working.steps.all()}
    seen = set()
    validated = []
    asset = working.instruction.source_asset
    for raw in raw_steps:
        allowed_fields(raw, {'id', 'title', 'description', 'start_ms', 'end_ms', 'evidence_segment_ids', 'evidence_frame_ids'})
        step_id = raw.get('id')
        if step_id is None:
            step_id = str(uuid.uuid4())
        elif not isinstance(step_id, str) or step_id not in existing:
            fail('VALIDATION_ERROR', 'Шаг не принадлежит этой рабочей версии.')
        if step_id in seen:
            fail('VALIDATION_ERROR', 'Один шаг передан несколько раз.')
        seen.add(step_id)
        start, end = raw.get('start_ms'), raw.get('end_ms')
        if type(start) is not int or type(end) is not int or start < 0 or end <= start or end > 600_000 or (asset and asset.duration_ms is not None and end > asset.duration_ms):
            fail('INVALID_TIME_RANGE', 'Конец шага должен быть позже начала и в пределах видео.')
        prior = existing.get(step_id)
        evidence = prior.evidence_segment_ids if prior else []
        if raw.get('evidence_segment_ids', evidence) != evidence:
            fail('VALIDATION_ERROR', 'Источники шага задаются при применении предложения ИИ.')
        frames = prior.evidence_frame_ids if prior else []
        if raw.get('evidence_frame_ids', frames) != frames:
            fail('VALIDATION_ERROR', 'Кадры-источники задаются при применении предложения ИИ.')
        value = {'id': step_id, 'title': text_field(raw, 'title', 3, 120), 'description': text_field(raw, 'description', 1, 2000), 'start_ms': start, 'end_ms': end}
        if prior:
            changed = any(value[key] != getattr(prior, key) for key in ['title', 'description', 'start_ms', 'end_ms'])
            value.update(origin='ai_edited' if changed and prior.origin in {'ai', 'ai_edited'} else prior.origin, evidence_segment_ids=evidence, evidence_transcript_revision=prior.evidence_transcript_revision, evidence_frame_ids=frames, evidence_visual_session_id=prior.evidence_visual_session_id, warning_codes=prior.warning_codes)
        validated.append(value)
    return validated


def save_working(member, instruction_id, data, supplied):
    allowed_fields(data, {'title', 'purpose', 'language', 'steps'})
    with transaction.atomic():
        instruction = instruction_for(member, instruction_id, lock=True)
        working = WorkingCopy.objects.select_for_update().get(instruction=instruction)
        require_match(working, supplied)
        if not working.is_open or working.workflow != 'draft':
            fail('INVALID_STATE', 'Верните инструкцию в черновик перед редактированием.')
        title, purpose, language = content_fields(data)
        steps = validate_steps(data, working)
        working.title, working.purpose, working.language = title, purpose, language
        # Rebuild order atomically. IDs stay stable; all review marks reset on editing.
        working.steps.all().delete()
        WorkingStep.objects.bulk_create([WorkingStep(organization=member.organization, working_copy=working, ordinal=i + 1, **step) for i, step in enumerate(steps)])
        bump(working)
        audit(member, 'working_copy.saved', instruction)
        return working_data(working)


def validate_ready(working):
    asset = working.instruction.source_asset
    if not asset or asset.status != 'ready' or asset.organization_id != working.organization_id:
        fail('INVALID_STATE', 'Для проверки требуется подготовленное видео.')
    count = working.steps.count()
    if not 1 <= count <= 30:
        fail('VALIDATION_ERROR', 'Добавьте от 1 до 30 шагов.')
    content_fields({'title': working.title, 'purpose': working.purpose, 'language': working.language})
    for step in working.steps.all():
        if step.end_ms > asset.duration_ms or step.end_ms <= step.start_ms:
            fail('INVALID_TIME_RANGE', 'Проверьте границы времени каждого шага.')
        text_field({'title': step.title}, 'title', 3, 120)
        text_field({'description': step.description}, 'description', 1, 2000)


def workflow_action(member, instruction_id, action, data, supplied):
    ensure_dict(data)
    with transaction.atomic():
        instruction = instruction_for(member, instruction_id, lock=True)
        working = WorkingCopy.objects.select_for_update().get(instruction=instruction)
        require_match(working, supplied)
        if not working.is_open:
            fail('INVALID_STATE', 'Создайте новый черновик для этой публикации.')
        if action == 'submit-review':
            allowed_fields(data, set())
            if working.workflow != 'draft':
                fail('INVALID_STATE', 'Инструкция уже на проверке.')
            validate_ready(working)
            working.workflow = 'in_review'
            working.return_reason = ''
            working.review_requested_by = member.user
            working.review_requested_at = timezone.now()
            working.steps.update(checked_by=None, checked_at=None)
        elif action in {'withdraw-review', 'return-to-draft'}:
            allowed_fields(data, {'reason'} if action == 'return-to-draft' else set())
            if working.workflow != 'in_review':
                fail('INVALID_STATE', 'Инструкция сейчас не находится на проверке.')
            if action == 'return-to-draft':
                working.return_reason = text_field(data, 'reason', 10, 2000)
            working.workflow = 'draft'
            working.review_requested_at = None
            working.review_requested_by = None
            working.steps.update(checked_by=None, checked_at=None)
        elif action == 'review/check-step':
            allowed_fields(data, {'step_id', 'checked'})
            if working.workflow != 'in_review':
                fail('INVALID_STATE', 'Сначала отправьте инструкцию на проверку.')
            if type(data.get('checked')) is not bool:
                fail('VALIDATION_ERROR', 'checked должен быть логическим значением.')
            try:
                step_id = uuid.UUID(str(data.get('step_id')))
            except (ValueError, TypeError):
                fail('VALIDATION_ERROR', 'Укажите корректный шаг.')
            step = working.steps.filter(id=step_id).first()
            if not step:
                fail('NOT_FOUND', 'Шаг не найден.', 404)
            step.checked_by = member.user if data['checked'] else None
            step.checked_at = timezone.now() if data['checked'] else None
            step.save()
        else:
            fail('NOT_FOUND', 'Действие не найдено.', 404)
        bump(working)
        audit(member, action.replace('/', '.'), instruction)
        return working_data(working)


def publish(member, instruction_id, data, supplied):
    allowed_fields(data, {'confirmations'})
    confirmations = data.get('confirmations')
    if not isinstance(confirmations, dict) or set(confirmations) != CONFIRMATIONS or any(value is not True for value in confirmations.values()):
        fail('VALIDATION_ERROR', 'Подтвердите все пять пунктов проверки.')
    instruction = instruction_for(member, instruction_id, lock=True)
    working = WorkingCopy.objects.select_for_update().get(instruction=instruction)
    require_match(working, supplied)
    if not working.is_open or working.workflow != 'in_review':
        fail('INVALID_STATE', 'Публикация доступна только после отправки на проверку.')
    validate_ready(working)
    if working.steps.filter(checked_by__isnull=True).exists():
        fail('VALIDATION_ERROR', 'Проверьте каждый шаг перед публикацией.')
    snapshot = {'title': working.title, 'purpose': working.purpose, 'language': working.language, 'steps': [step_data(s) for s in working.steps.all()], 'source': source_data(instruction.source_asset), 'confirmations': confirmations}
    number = (instruction.versions.aggregate(value=Max('version_number'))['value'] or 0) + 1
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    version = PublishedVersion.objects.create(organization=member.organization, instruction=instruction, source_asset=instruction.source_asset, version_number=number, snapshot=snapshot, content_sha256=digest, source_working_revision=working.revision, published_by=member.user)
    instruction.current_version = version
    instruction.published_search_text = ' '.join([working.title, working.purpose, *[s.title + ' ' + s.description for s in working.steps.all()]])
    instruction.save()
    working.is_open = False
    bump(working)
    audit(member, 'instruction.published', instruction)
    return version_data(version), 201


def create_draft(member, instruction_id, data):
    allowed_fields(data, {'source_version_id'})
    with transaction.atomic():
        instruction = instruction_for(member, instruction_id, lock=True)
        working = WorkingCopy.objects.select_for_update().get(instruction=instruction)
        if working.is_open:
            fail('INVALID_STATE', 'Для инструкции уже есть открытый черновик.', 409)
        try:
            version_id = uuid.UUID(str(data.get('source_version_id')))
        except (ValueError, TypeError):
            fail('VALIDATION_ERROR', 'Выберите исходную версию.')
        version = instruction.versions.filter(id=version_id, organization=member.organization).first()
        if not version:
            fail('NOT_FOUND', 'Версия не найдена.', 404)
        snapshot = version.snapshot
        working.title, working.purpose, working.language = snapshot['title'], snapshot['purpose'], snapshot['language']
        working.workflow, working.is_open = 'draft', True
        working.based_on_version = version
        working.return_reason = ''
        working.review_requested_at = None
        working.review_requested_by = None
        working.steps.all().delete()
        for i, source in enumerate(snapshot['steps']):
            values = {key: source[key] for key in ['id', 'title', 'description', 'start_ms', 'end_ms']}
            values.update({key: source[key] for key in ['origin', 'evidence_segment_ids', 'evidence_transcript_revision', 'evidence_frame_ids', 'evidence_visual_session_id', 'warning_codes'] if key in source})
            WorkingStep.objects.create(organization=member.organization, working_copy=working, ordinal=i + 1, **values)
        bump(working)
        audit(member, 'draft.created', instruction)
        return working_data(working)


def withdraw_publication(member, instruction_id, data):
    allowed_fields(data, {'reason', 'current_version_id'})
    text_field(data, 'reason', 10, 2000)
    with transaction.atomic():
        instruction = instruction_for(member, instruction_id, lock=True)
        if not instruction.current_version_id or str(instruction.current_version_id) != data.get('current_version_id'):
            fail('REVISION_CONFLICT', 'Текущая публикация изменилась.', 409)
        instruction.current_version = None
        instruction.published_search_text = ''
        instruction.save()
        audit(member, 'publication.withdrawn', instruction)
        return summary(instruction)
