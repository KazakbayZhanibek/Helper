import hashlib
import io
import os
import uuid
import warnings
from datetime import timedelta
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from ai_drafts.services import fingerprint
from ai_drafts.providers import ProviderFailure
from ai_drafts.validation import InvalidModelOutput
from core import services as s
from core.models import Organization, WorkingCopy, WorkingStep
from media_pipeline.jobs import enqueue_job, job_data
from media_pipeline.models import ProcessingJob
from .models import FrameEvidence, VisualProposal, VisualSession, VisualAnalysisWindow
from .planning import analysis_data, intervals
from .provider import LIMITS, capabilities

FRAME_STORAGE_LIMIT = 512 * 1024 * 1024


def cloud_ready(organization):
    config = capabilities(organization)
    if not config['available']:
        s.fail('VISUAL_AI_DISABLED', config['reason'], 503)
    return config


def lock_working(member, instruction_id, supplied):
    instruction = s.instruction_for(member, instruction_id, lock=True)
    working = WorkingCopy.objects.select_for_update().get(instruction=instruction)
    s.require_match(working, supplied)
    if not working.is_open or working.workflow != 'draft':
        s.fail('INVALID_STATE', 'Для анализа верните рабочую версию в черновик.', 409)
    return instruction, working


def require_consent(data):
    if data.get('consent_images') is not True:
        s.fail('IMAGE_CONSENT_REQUIRED', 'Подтвердите передачу кадров облачному ИИ для анализа.')


def session_for(member, session_id, *, lock=False, owner=False):
    query = VisualSession.objects.select_related('instruction', 'asset', 'organization', 'created_by').filter(organization=member.organization, instruction__lifecycle='active')
    if lock:
        query = query.select_for_update(of=('self',))
    session = query.filter(id=session_id).first()
    if not session:
        s.fail('NOT_FOUND', 'Сеанс не найден.', 404)
    if owner and session.created_by_id != member.user_id:
        s.fail('FORBIDDEN', 'Только автор этого сеанса может передавать и анализировать кадры.', 403)
    return session


def require_active(session):
    if session.state != 'active' or session.expires_at <= timezone.now() or not session.consent_images:
        s.fail('VISUAL_SESSION_CLOSED', 'Сеанс остановлен или истёк. Начните новый сеанс.', 409)


def frame_data(frame):
    return {'id': str(frame.id), 'sequence': frame.sequence, 'timestamp_ms': frame.timestamp_ms, 'width': frame.width, 'height': frame.height, 'url': f'/api/v1/orgs/{frame.organization_id}/visual-frames/{frame.id}'}


def evidence_ids(content):
    """All persisted evidence, including states/gaps that cannot become steps."""
    refs = {ref for step in content['steps'] for ref in step['evidence_frame_ids']}
    if content.get('schema_version') == '3':
        for observation in content.get('observations', []):
            refs.update(observation.get('evidence_frame_ids', []))
            refs.update(observation.get('action_frame_ids', []))
            refs.update(ref for ref in (observation.get('before_frame_id'), observation.get('after_frame_id')) if ref is not None)
        for gap in content.get('gaps', []):
            refs.update(gap.get('evidence_frame_ids', []))
        for pair in content.get('temporal_coverage', {}).get('pairs', []):
            refs.update((pair['from_frame_id'], pair['to_frame_id']))
        if 'frame_grounding' in content:
            try:
                ledger = content['frame_grounding']
                if (not isinstance(ledger, dict) or ledger.get('version') not in ('v1', 'v2')
                        or not isinstance(ledger.get('batches'), list)):
                    raise ValueError()
                for batch in ledger['batches']:
                    if not isinstance(batch, dict) or not isinstance(batch.get('frame_ids'), list):
                        raise ValueError()
                    extra = list(batch['frame_ids'])
                    if ledger['version'] == 'v1':
                        if not isinstance(batch.get('states'), list):
                            raise ValueError()
                        extra.extend(state['frame_id'] for state in batch['states'])
                    if any(not isinstance(ref, str) or str(uuid.UUID(ref)) != ref for ref in extra):
                        raise ValueError()
                    refs.update(extra)
            except (KeyError, TypeError, ValueError, AttributeError):
                raise InvalidModelOutput('VISUAL_GROUNDING_INVALID') from None
    return refs


def checked_instruction_content(content, frames, session):
    """Same read/apply projection; no model calls, fixes or history mutation."""
    from .short_actions import reconcile_action_links
    pin = session.analysis_budget.get('frame_grounding')
    ledger = content.get('frame_grounding', {})
    if not isinstance(ledger, dict):
        raise InvalidModelOutput('VISUAL_GROUNDING_PROTOCOL_INVALID')
    actual = ledger.get('version')
    if (pin not in (None, 'v1', 'v2') or pin != actual
            or (pin and (session.mode != 'upload' or session.analysis_budget.get('visual_protocol') != '3'
                         or session.analysis_budget.get('short_action_coverage') != 'v1'
                         or session.analysis_budget.get('semantic_review')))):
        raise InvalidModelOutput('VISUAL_GROUNDING_PROTOCOL_INVALID')
    if pin:
        if pin == 'v2':
            from .grounding_v2 import reconcile_grounding
        else:
            from .grounding import reconcile_grounding
        # Validate associations of every claim before the temporal filter can
        # remove uncertain/unlinked observations and hide a corrupt reference.
        return reconcile_grounding(content, frames, session)
    return reconcile_action_links(content, frames, session)


def action_links_valid(content, frames):
    """Check projection identity and frame links, not the truth of model claims."""
    if content.get('schema_version') != '3':
        return True
    try:
        from .teaching import STEP_FORMAT, describe_action
        step_format = content.get('step_format')
        if step_format not in {None, STEP_FORMAT}:
            return False
        observations = content['observations']
        by_id = {observation['id']: observation for observation in observations}
        if len(by_id) != len(observations):
            return False
        if any(observation['kind'] not in {'action', 'state'} for observation in observations):
            return False
        if not evidence_ids(content).issubset(frames):
            return False
        linked = set()
        for step in content['steps']:
            links = step.get('observation_ids', [])
            refs = step['evidence_frame_ids']
            if not links or len(set(links)) != len(links) or len(refs) < 2 or len(set(refs)) != len(refs):
                return False
            ordered = sorted(refs, key=lambda ref: frames[ref].sequence)
            if refs != ordered or step['start_ms'] != min(frames[ref].coverage_start_ms for ref in refs) or step['end_ms'] != max(frames[ref].coverage_end_ms for ref in refs):
                return False
            for ref in links:
                observation = by_id[ref]
                if observation['kind'] != 'action' or observation['title'] != step['title'] or describe_action(observation, language=content['language'], step_format=step_format) != step['description'] or observation['action_frame_ids'] != refs:
                    return False
                if not set(refs).issubset(observation['evidence_frame_ids']):
                    return False
            linked.update(links)
        return linked == {ref for ref, observation in by_id.items() if observation['kind'] == 'action'}
    except (KeyError, TypeError, ValueError, InvalidModelOutput):
        return False


def proposal_data(proposal):
    session = proposal.session
    working = session.instruction.working_copy
    stale = working.revision != session.base_working_revision or (session.mode == 'upload' and session.instruction.source_asset_id != session.asset_id) or bool(session.frames_purged_at)
    preliminary = session.mode == 'live'
    try:
        ids = evidence_ids(proposal.content)
        frames = list(session.frames.filter(id__in=ids))
        links_valid = action_links_valid(proposal.content, {str(frame.id): frame for frame in frames})
    except InvalidModelOutput:
        frames, links_valid = [], False
    content = dict(proposal.content)
    if links_valid and proposal.applied_at is None and session.mode == 'upload':
        try:
            content = checked_instruction_content(content, frames, session)
            links_valid = action_links_valid(content, {str(frame.id): frame for frame in frames})
        except InvalidModelOutput:
            links_valid = False
    if isinstance(content.get('analysis'), dict):
        from ai_budget.services import budget_snapshot
        content['analysis'] = {**content['analysis'], 'budget': budget_snapshot(session)}
    if links_valid and not preliminary and proposal.applied_at is None:
        from .teaching import present_instruction
        content = present_instruction(content)
    return {'id': str(proposal.id), 'instruction_id': str(session.instruction_id), 'session_id': str(session.id), 'base_working_revision': session.base_working_revision, 'provider': proposal.provider, 'model': proposal.model, 'created_at': s.utc(proposal.created_at), 'applied_at': s.utc(proposal.applied_at), 'preliminary': preliminary, 'can_apply': not preliminary and not stale and links_valid and proposal.applied_at is None and working.is_open and working.workflow == 'draft' and 0 < len(content['steps']) <= 30, 'stale': bool(stale), 'window_start_ms': session.window_start_ms, 'window_end_ms': session.window_end_ms, 'frames': [frame_data(f) for f in frames], **content, 'source': s.source_data(session.asset)}


def session_data(session):
    state = 'expired' if session.state == 'active' and session.expires_at <= timezone.now() else session.state
    frames = list(session.frames.order_by('-sequence')[:12])
    observations = session.proposals.select_related('session__instruction__working_copy', 'session__asset').order_by('-created_at')[:5]
    return {'id': str(session.id), 'instruction_id': str(session.instruction_id), 'asset_id': str(session.asset_id) if session.asset_id else None, 'mode': session.mode, 'state': state, 'base_working_revision': session.base_working_revision, 'created_at': s.utc(session.created_at), 'expires_at': s.utc(session.expires_at), 'frame_count': session.frames.count(), 'analysis': analysis_data(session) if session.analysis_windows.exists() else None, 'frames': [frame_data(f) for f in reversed(frames)], 'observations': [proposal_data(p) for p in observations]}


@transaction.atomic
def start_live(member, instruction_id, data, supplied):
    s.allowed_fields(data, {'consent_images'})
    require_consent(data)
    Organization.objects.select_for_update().get(pk=member.organization_id)
    instruction, working = lock_working(member, instruction_id, supplied)
    VisualSession.objects.filter(organization=member.organization, state='active', expires_at__lte=timezone.now()).update(state='expired')
    if VisualSession.objects.filter(instruction=instruction, mode='live', state='active').exists():
        s.fail('VISUAL_SESSION_EXISTS', 'Для инструкции уже запущена камера. Остановите предыдущий сеанс.', 409)
    if VisualSession.objects.filter(organization=member.organization, mode='live', state='active').count() >= 3:
        s.fail('VISUAL_SESSION_LIMIT', 'В организации уже работают три камеры.', 429)
    protocol = {'visual_protocol': '3'} if getattr(settings, 'VISUAL_ACTION_EVIDENCE', False) else {}
    session = VisualSession.objects.create(organization=member.organization, instruction=instruction, asset=None, created_by=member.user, mode='live', consent_images=True, base_working_revision=working.revision, language=working.language, expires_at=timezone.now() + timedelta(minutes=10), analysis_budget=protocol)
    s.audit(member, 'visual_session.started', session)
    return session_data(session), 201


def decoded_jpeg(raw, *, max_input_bytes=LIMITS['max_frame_bytes']):
    if not raw or len(raw) > max_input_bytes:
        s.fail('FRAME_TOO_LARGE', 'Кадр должен быть JPEG не больше 512 KiB.', 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != 'JPEG' or image.width * image.height > 16_000_000 or image.width > 4096 or image.height > 4096:
                    s.fail('INVALID_FRAME', 'Допустим только JPEG размером до 4096 × 4096.', 422)
                image.load()
                image = image.convert('RGB')
                image.thumbnail((960, 960))
                target = io.BytesIO()
                image.save(target, format='JPEG', quality=82, optimize=True)
                value = target.getvalue()
                if len(value) > LIMITS['max_frame_bytes']:
                    s.fail('FRAME_TOO_LARGE', 'Кадр после подготовки превышает 512 KiB.', 413)
                return value, image.width, image.height
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        s.fail('INVALID_FRAME', 'Не удалось прочитать JPEG-кадр.', 422)


def safe_frame_path(key):
    root = Path(settings.PRIVATE_MEDIA_ROOT).resolve()
    path = (root / key).resolve()
    if not path.is_relative_to(root):
        s.fail('NOT_FOUND', 'Кадр не найден.', 404)
    return path


def persist_frame(session, *, sequence, timestamp_ms, coverage_start_ms, coverage_end_ms, content, width, height, idempotency_key, request_sha256, frame_id=None):
    # Caller holds the organization row lock, including workers, so quota cannot
    # be raced by simultaneous camera uploads and extracted video frames.
    used = FrameEvidence.objects.filter(organization_id=session.organization_id).aggregate(total=Sum('size_bytes'))['total'] or 0
    if used + len(content) > FRAME_STORAGE_LIMIT:
        s.fail('VISUAL_STORAGE_QUOTA', 'Лимит хранения кадров организации достигнут. Очистите старые сеансы.', 429)
    frame_id = frame_id or uuid.uuid4()
    key = f'visual-evidence/{session.organization_id}/{session.id}/{frame_id}.jpg'
    path = safe_frame_path(key)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
        return FrameEvidence.objects.create(id=frame_id, organization_id=session.organization_id, session=session, sequence=sequence, timestamp_ms=timestamp_ms, coverage_start_ms=coverage_start_ms, coverage_end_ms=coverage_end_ms, width=width, height=height, size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(), request_sha256=request_sha256, idempotency_key=idempotency_key, storage_key=key)
    except Exception:
        path.unlink(missing_ok=True)
        raise


def receive_frame(member, session_id, stream, *, sequence, timestamp_ms, key):
    if not key or len(key) > 128:
        s.fail('VALIDATION_ERROR', 'Для кадра нужен Idempotency-Key до 128 символов.')
    if type(sequence) is not int or type(timestamp_ms) is not int or sequence < 1 or sequence > 300 or not 0 <= timestamp_ms < 600000:
        s.fail('VALIDATION_ERROR', 'Некорректный номер или время кадра.')
    # Bound the read irrespective of a client-provided Content-Length.
    raw = stream.read(LIMITS['max_frame_bytes'] + 1)
    if len(raw) > LIMITS['max_frame_bytes']:
        s.fail('FRAME_TOO_LARGE', 'Максимальный размер кадра — 512 KiB.', 413)
    digest = hashlib.sha256(raw).hexdigest()
    session = session_for(member, session_id, owner=True)
    previous = _check_frame_receipt(session, sequence, timestamp_ms, key, digest)
    if previous:
        return frame_data(previous), 200
    # Decode before holding any organization/session row lock. Re-check all
    # mutable state under the lock afterwards (including a concurrent retry).
    content, width, height = decoded_jpeg(raw)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=member.organization_id)
        member = s.membership(member.user, member.organization_id, s.WRITERS)
        session = session_for(member, session_id, lock=True, owner=True)
        previous = _check_frame_receipt(session, sequence, timestamp_ms, key, digest)
        if previous:
            return frame_data(previous), 200
        frame = persist_frame(session, sequence=sequence, timestamp_ms=timestamp_ms, coverage_start_ms=timestamp_ms, coverage_end_ms=min(600000, timestamp_ms + 1000), content=content, width=width, height=height, idempotency_key=key, request_sha256=digest)
        return frame_data(frame), 201


def _check_frame_receipt(session, sequence, timestamp_ms, key, digest):
    if session.mode != 'live':
        s.fail('INVALID_STATE', 'Передача кадров доступна только для живой камеры.', 409)
    previous = session.frames.filter(idempotency_key=key).first()
    if previous:
        if previous.sequence != sequence or previous.timestamp_ms != timestamp_ms or previous.request_sha256 != digest:
            s.fail('IDEMPOTENCY_KEY_REUSED', 'Ключ уже использован для другого кадра.', 409)
        return previous
    require_active(session)
    latest = session.frames.order_by('-sequence').first()
    if sequence != (latest.sequence + 1 if latest else 1):
        s.fail('FRAME_SEQUENCE_CONFLICT', 'Передайте следующий кадр по порядку.', 409)
    if latest and (timestamp_ms - latest.timestamp_ms < 1000 or (timezone.now() - latest.created_at).total_seconds() < 1):
        s.fail('FRAME_RATE_LIMIT', 'Принимайте не больше одного кадра в секунду.', 429)
    elapsed = (timezone.now() - session.created_at).total_seconds() * 1000
    if timestamp_ms > elapsed + 5000:
        s.fail('INVALID_FRAME_TIME', 'Время кадра выходит за пределы текущей записи.', 422)
    return None


@transaction.atomic
def analyze_live(member, session_id, data):
    s.allowed_fields(data, set())
    Organization.objects.select_for_update().get(pk=member.organization_id)
    session = session_for(member, session_id, lock=True, owner=True)
    require_active(session)
    if session.mode != 'live':
        s.fail('INVALID_STATE', 'Ожидается сеанс живой камеры.', 409)
    cloud_ready(member.organization)
    if session.last_analysis_at and (timezone.now() - session.last_analysis_at).total_seconds() < 8:
        s.fail('VISUAL_ANALYSIS_RATE_LIMIT', 'Новый анализ доступен через несколько секунд.', 429)
    frames = list(session.frames.order_by('-sequence')[:12])
    if len(frames) < 2:
        s.fail('NO_VISUAL_FRAMES', 'Для наблюдения действий нужны минимум два кадра с камеры.', 422)
    if session.analysis_calls >= 60 or ProcessingJob.objects.filter(visual_session_id=session.id, kind='analyze_live').count() >= 60:
        s.fail('VISUAL_CALL_LIMIT', 'Достигнут лимит 60 анализов для одного сеанса.', 429)
    ids = [str(frame.id) for frame in reversed(frames)]
    payload = {'visual_session_id': str(session.id), 'frame_ids': ids, 'visual_protocol': session.analysis_budget.get('visual_protocol', '2')}
    job = enqueue_job(member, session.instruction, 'analyze_live', payload=payload, input_hash=fingerprint(payload))
    session.last_analysis_at = timezone.now()
    session.save(update_fields=['last_analysis_at', 'updated_at'])
    return job_data(job), 202


@transaction.atomic
def request_uploaded(member, instruction_id, data, supplied):
    from ai_budget.services import budget_capabilities, preflight
    s.allowed_fields(data, {'consent_images', 'start_ms', 'end_ms', 'scope', 'max_ai_calls', 'max_cost_microusd'})
    require_consent(data)
    config = cloud_ready(member.organization)
    Organization.objects.select_for_update().get(pk=member.organization_id)
    instruction, working = lock_working(member, instruction_id, supplied)
    asset = instruction.source_asset
    if not asset or asset.status != 'ready':
        s.fail('MEDIA_NOT_READY', 'Сначала загрузите и подготовьте видео.', 409)
    scope = data.get('scope', 'window' if 'start_ms' in data or 'end_ms' in data else 'full')
    if scope not in {'full', 'window'}:
        s.fail('INVALID_VISUAL_SCOPE', 'Выберите всю запись или отдельный фрагмент.', 422)
    if scope == 'full':
        if 'start_ms' in data or 'end_ms' in data:
            s.fail('INVALID_VISUAL_WINDOW', 'Для всей записи не задавайте границы фрагмента.', 422)
        start, end = 0, asset.duration_ms
        if not end or end > 600000:
            s.fail('INVALID_VISUAL_WINDOW', 'Полный анализ поддерживает видео длительностью до 10 минут.', 422)
        windows = intervals(start, end)
    else:
        start, end = data.get('start_ms', 0), data.get('end_ms', asset.duration_ms)
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= asset.duration_ms or end - start > 60000:
            s.fail('INVALID_VISUAL_WINDOW', 'Для одного фрагмента выберите отрывок до 60 секунд в пределах видео.', 422)
        windows = [(start, end)]
    limits = budget_capabilities(config['provider'], config['model'])
    calls = data.get('max_ai_calls', limits.get('default_max_calls', 30))
    cost = data.get('max_cost_microusd', limits.get('default_max_cost_microusd') or 0)
    if type(calls) is not int or not 1 <= calls <= limits.get('max_calls', 60):
        s.fail('INVALID_AI_BUDGET', 'Лимит обращений выходит за разрешённые границы.', 422)
    if type(cost) is not int or not 0 <= cost <= (limits.get('max_cost_microusd') or 0):
        s.fail('INVALID_AI_BUDGET', 'Денежный лимит выходит за разрешённые границы.', 422)
    if calls < len(windows):
        s.fail('AI_BUDGET_TOO_SMALL', f'Для первого просмотра всей выбранной записи нужно минимум {len(windows)} обращений. Увеличьте лимит или выберите фрагмент.', 422)
    protocol = {'visual_protocol': '3'} if getattr(settings, 'VISUAL_ACTION_EVIDENCE', False) else {}
    if protocol and getattr(settings, 'VISUAL_SHORT_ACTION_COVERAGE', False):
        protocol['short_action_coverage'] = 'v1'
        if getattr(settings, 'VISUAL_FRAME_GROUNDING', False):
            version = getattr(settings, 'VISUAL_FRAME_GROUNDING_VERSION', 'v1')
            if version not in ('v1', 'v2'):
                s.fail('VISUAL_PROTOCOL_INVALID', 'Неизвестный режим проверки кадров. Проверьте настройки сервера.', 422)
            protocol['frame_grounding'] = version
    elif protocol and getattr(settings, 'VISUAL_SEMANTIC_REVIEW', False):
        protocol['semantic_review'] = 'v1'
    session = VisualSession.objects.create(organization=member.organization, instruction=instruction, asset=asset, created_by=member.user, mode='upload', consent_images=True, base_working_revision=working.revision, language=working.language, expires_at=timezone.now() + timedelta(hours=6), window_start_ms=start, window_end_ms=end, analysis_scope=scope, analysis_budget={'max_calls': calls, 'max_cost_microusd': cost, **protocol})
    try:
        preflight(session, len(windows))
    except ProviderFailure as error:
        s.fail(error.code, error.message, 422)
    VisualAnalysisWindow.objects.bulk_create([VisualAnalysisWindow(organization=member.organization, session=session, ordinal=index + 1, start_ms=left, end_ms=right) for index, (left, right) in enumerate(windows)])
    payload = {'visual_session_id': str(session.id), 'visual_protocol': session.analysis_budget.get('visual_protocol', '2')}
    if protocol.get('semantic_review'):
        payload['semantic_review'] = protocol['semantic_review']
    if protocol.get('short_action_coverage'):
        payload['short_action_coverage'] = protocol['short_action_coverage']
    if protocol.get('frame_grounding'):
        payload['frame_grounding'] = protocol['frame_grounding']
    job = enqueue_job(member, instruction, 'analyze_video', payload=payload, input_hash=fingerprint({**payload, 'start_ms': start, 'end_ms': end, 'working_revision': working.revision}))
    job.result = {'visual_session_id': str(session.id), 'analysis': analysis_data(session)}
    job.save(update_fields=['result', 'updated_at'])
    return job_data(job), 202


@transaction.atomic
def cancel_session(member, session_id):
    Organization.objects.select_for_update().get(pk=member.organization_id)
    session = session_for(member, session_id, lock=True)
    if session.created_by_id != member.user_id and member.role != 'admin':
        s.fail('FORBIDDEN', 'Остановить камеру может автор сеанса или администратор.', 403)
    if session.state == 'active':
        session.state = 'cancelled'
        session.save(update_fields=['state', 'updated_at'])
    queued = ProcessingJob.objects.filter(organization=member.organization, visual_session_id=session.id, kind__in=['analyze_video', 'analyze_live'], state='queued')
    queued.update(state='cancelled', stage='cancelled', cancel_requested=True, retryable=False, finished_at=timezone.now())
    ProcessingJob.objects.filter(organization=member.organization, visual_session_id=session.id, state='running').update(cancel_requested=True)
    s.audit(member, 'visual_session.cancelled', session)
    return session_data(session)


def proposal_for(member, instruction_id, proposal_id, *, lock=False):
    instruction = s.instruction_for(member, instruction_id, lock=lock)
    query = VisualProposal.objects.select_related('session__instruction__working_copy', 'session__asset').filter(organization=member.organization, session__instruction=instruction)
    if lock:
        query = query.select_for_update(of=('self',))
    proposal = query.filter(id=proposal_id).first()
    if not proposal:
        s.fail('NOT_FOUND', 'Визуальное предложение не найдено.', 404)
    return proposal


@transaction.atomic
def apply_proposal(member, instruction_id, proposal_id, data, supplied):
    s.allowed_fields(data, {'confirm_replace', 'mode'})
    mode = data.get('mode', 'replace')
    if data.get('confirm_replace') is not True or mode not in {'replace', 'append'}:
        s.fail('REPLACEMENT_CONFIRMATION_REQUIRED', 'Подтвердите добавление или замену шагов предложением ИИ.')
    proposal = proposal_for(member, instruction_id, proposal_id, lock=True)
    session = VisualSession.objects.select_for_update().get(pk=proposal.session_id)
    if session.mode != 'upload':
        s.fail('LIVE_PROPOSAL_READ_ONLY', 'Наблюдения камеры предварительные. Сохраните видео и проанализируйте запись.', 409)
    working = WorkingCopy.objects.select_for_update().get(instruction_id=instruction_id)
    if not supplied:
        s.fail('PRECONDITION_REQUIRED', 'Нужна актуальная версия инструкции.', 428)
    if supplied != s.etag(working) or session.base_working_revision != working.revision or proposal.applied_at or session.frames_purged_at or session.state != 'completed' or session.instruction.source_asset_id != session.asset_id:
        s.fail('STALE_PROPOSAL', 'Предложение устарело. Текущие шаги сохранены.', 409)
    if not working.is_open or working.workflow != 'draft':
        s.fail('INVALID_STATE', 'Верните инструкцию в черновик.', 409)
    try:
        frame_ids = evidence_ids(proposal.content)
    except InvalidModelOutput:
        s.fail('VISUAL_EVIDENCE_INVALID', 'Не удалось проверить сведения об исходных кадрах.', 422)
    frames = {str(frame.id): frame for frame in session.frames.filter(id__in=frame_ids)}
    if len(frames) != len(frame_ids) or not action_links_valid(proposal.content, frames):
        s.fail('VISUAL_EVIDENCE_INVALID', 'Исходные кадры предложения недоступны.', 422)
    try:
        content = checked_instruction_content(proposal.content, list(frames.values()), session)
    except InvalidModelOutput:
        s.fail('VISUAL_EVIDENCE_INVALID', 'Не удалось согласовать действие с исходными кадрами.', 422)
    if not action_links_valid(content, frames):
        s.fail('VISUAL_EVIDENCE_INVALID', 'Не удалось согласовать действие с исходными кадрами.', 422)
    from .teaching import present_instruction
    steps = present_instruction(content)['steps']
    if not steps:
        s.fail('NO_PROPOSED_STEPS', 'В предложении нет шагов для применения.', 422)
    old_count = working.steps.count() if mode == 'append' else 0
    if old_count + len(steps) > 30:
        s.fail('STEP_LIMIT', 'В рабочей инструкции допускается не больше 30 шагов.', 422)
    if mode == 'replace':
        working.steps.all().delete()
    else:
        working.steps.update(checked_at=None, checked_by=None)
    warning_codes = sorted({w['code'] for w in content['warnings']})
    fields = ('title', 'description', 'start_ms', 'end_ms', 'evidence_frame_ids')
    WorkingStep.objects.bulk_create([WorkingStep(organization=member.organization, working_copy=working, ordinal=old_count + index + 1, origin='ai', evidence_visual_session_id=session.id, warning_codes=warning_codes, **{field: step[field] for field in fields}) for index, step in enumerate(steps)])
    s.bump(working)
    proposal.applied_at, proposal.applied_by = timezone.now(), member.user
    proposal.save(update_fields=['applied_at', 'applied_by', 'updated_at'])
    s.audit(member, 'visual_proposal.applied', proposal)
    return s.working_data(working), 200
