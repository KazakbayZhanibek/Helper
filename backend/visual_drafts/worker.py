"""Vision creates independent, unapproved evidence-backed proposals only."""
import hashlib
import uuid
from fractions import Fraction

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from ai_drafts.providers import ProviderFailure
from ai_drafts.validation import InvalidModelOutput
from core.http import DomainError
from core.models import Membership, Organization
from media_pipeline.exceptions import ProcessingFailure
from media_pipeline.frames import extract_video_frames
from media_pipeline.telemetry import count, measured, with_metrics
from .models import FrameEvidence, VisualProposal, VisualSession, VisualAnalysisWindow
from .planning import analysis_data, aggregate_content, intervals, needs_refinement, REFINEMENT_WINDOW_MS
from ai_budget.services import execute_budgeted, preflight
from .provider import VisualProvider, capabilities, validate
from .services import decoded_jpeg, persist_frame, safe_frame_path


def protocol_for_job(job, session):
    """A session keeps its contract across deploys, retries and live batches."""
    protocol = job.payload.get('visual_protocol', '2')
    pinned = session.analysis_budget.get('visual_protocol', '2')
    if protocol not in ('2', '3') or protocol != pinned:
        raise ProcessingFailure('VISUAL_PROTOCOL_INVALID', 'Режим задания не соответствует сеансу анализа. Начните новый сеанс.')
    review = session.analysis_budget.get('semantic_review')
    if (review != job.payload.get('semantic_review') or review not in (None, 'v1')
            or (review and (protocol != '3' or session.mode != 'upload'))):
        raise ProcessingFailure('VISUAL_PROTOCOL_INVALID', 'Режим проверки не соответствует сеансу анализа. Начните новый сеанс.')
    coverage = session.analysis_budget.get('short_action_coverage')
    if (coverage != job.payload.get('short_action_coverage') or coverage not in (None, 'v1')
            or (coverage and (protocol != '3' or session.mode != 'upload' or review))):
        raise ProcessingFailure('VISUAL_PROTOCOL_INVALID', 'Режим разбора переходов не соответствует сеансу анализа.')
    grounding = session.analysis_budget.get('frame_grounding')
    if (grounding != job.payload.get('frame_grounding') or grounding not in (None, 'v1', 'v2')
            or (grounding and (protocol != '3' or session.mode != 'upload' or coverage != 'v1' or review))):
        raise ProcessingFailure('VISUAL_PROTOCOL_INVALID', 'Режим проверки фактов кадров не соответствует сеансу анализа.')
    return protocol


def check_grounding_checkpoint(content, session, *, window=False):
    """A completed result cannot silently lose or acquire its pinned contract."""
    expected = session.analysis_budget.get('frame_grounding')
    grounding = content.get('frame_grounding')
    if (grounding is not None and not isinstance(grounding, dict)):
        raise ProcessingFailure('VISUAL_PROTOCOL_INVALID', 'Сохранённая проверка кадров повреждена.')
    if ((grounding or {}).get('version') != expected
            or (expected and (not isinstance(grounding.get('batches'), list)
                              or (window and len(grounding['batches']) != 1)))):
        raise ProcessingFailure('VISUAL_PROTOCOL_INVALID', 'Сохранённый результат использует другой режим проверки кадров.')
    if expected == 'v2':
        required = ('frame_ids', 'actions', 'cut_after_image_indices', 'rejected_observation_ids')
        if any(not isinstance(batch, dict) or not isinstance(batch.get('id'), str) or not batch['id']
               or any(not isinstance(batch.get(name), list) for name in required)
               for batch in grounding['batches']):
            raise ProcessingFailure('VISUAL_PROTOCOL_INVALID', 'Сохранённая проверка кадров повреждена.')


def provider_for_job(job, session, *, images=None):
    provider = VisualProvider(session.organization)
    provider.protocol = protocol_for_job(job, session)
    provider.short_action_coverage = session.analysis_budget.get('short_action_coverage')
    provider.frame_grounding = session.analysis_budget.get('frame_grounding')
    if provider.protocol == '3' and images is not None:
        # A deterministic local input/schema failure must not consume a receipt
        # or be mistaken for an unknown dispatched model call.
        provider.prepare_request(language=session.language, mode=session.mode, frames=images)
    return provider


def active_session(job, *, lock=False, allow_completed=False):
    try:
        session_id = uuid.UUID(str(job.payload.get('visual_session_id', '')))
    except (ValueError, TypeError):
        raise ProcessingFailure('VISUAL_SESSION_INVALID', 'Не удалось определить сеанс анализа.') from None
    if job.visual_session_id is not None and job.visual_session_id != session_id:
        raise ProcessingFailure('VISUAL_SESSION_INVALID', 'Сеанс задания не соответствует сохранённой связи.')
    query = VisualSession.objects.select_related('organization', 'instruction', 'asset').filter(id=session_id, organization_id=job.organization_id, instruction_id=job.instruction_id, created_by_id=job.requested_by_id, instruction__lifecycle='active', organization__status='active', consent_images=True, frames_purged_at__isnull=True)
    if lock:
        query = query.select_for_update(of=('self',))
    session = query.first()
    permitted_states = {'active', 'completed'} if allow_completed else {'active'}
    if not session or session.state not in permitted_states or (session.state == 'active' and session.expires_at <= timezone.now()):
        raise ProcessingFailure('VISUAL_SESSION_CLOSED', 'Сеанс остановлен или истёк. Поздний результат не сохранён.')
    if not Membership.objects.filter(organization_id=job.organization_id, user_id=job.requested_by_id, user__is_active=True, is_active=True, role__in=['admin', 'author', 'reviewer']).exists():
        raise ProcessingFailure('VISUAL_ACCESS_REVOKED', 'Доступ автора к анализу отозван.')
    expected_mode = 'live' if job.kind == 'analyze_live' else 'upload'
    if session.mode != expected_mode or (expected_mode == 'live' and job.asset_id is not None) or (expected_mode == 'upload' and (session.asset_id != job.asset_id or session.instruction.source_asset_id != job.asset_id or not session.asset or session.asset.status != 'ready')):
        raise ProcessingFailure('VISUAL_SOURCE_MISMATCH', 'Источник кадров не соответствует заданию.')
    protocol_for_job(job, session)
    config = capabilities(session.organization)
    if not config['available']:
        raise ProcessingFailure('VISUAL_AI_DISABLED', config['reason'])
    return session


def required_parent_frames(session, window):
    """Retain exact decoded PTS, not approximate seek times, when looking closer."""
    if (not window or not window.parent_id or session.mode != 'upload'
            or session.analysis_budget.get('visual_protocol') != '3'
            or session.analysis_budget.get('short_action_coverage') != 'v1'):
        return []
    parent = VisualAnalysisWindow.objects.filter(
        pk=window.parent_id, session_id=session.id, organization_id=session.organization_id,
        state='completed', parent__isnull=True,
    ).first()
    message = 'Исходные кадры подробного просмотра не удалось проверить.'
    if not parent or not parent.frame_ids or not parent.start_ms <= window.start_ms < window.end_ms <= parent.end_ms:
        raise ProcessingFailure('VISUAL_PARENT_EVIDENCE_INVALID', message)
    frames = list(session.frames.filter(id__in=parent.frame_ids, organization_id=session.organization_id).order_by('timestamp_ms'))
    if len(frames) != len(parent.frame_ids) or not 2 <= len(frames) <= 12:
        raise ProcessingFailure('VISUAL_PARENT_EVIDENCE_INVALID', message)
    # Check immutable stored bytes before extracting another view of them.
    readable_frames(frames)
    manifest = parent.source_manifest
    try:
        if (manifest['asset_id'] != str(session.asset_id)
                or manifest['start_ms'] != parent.start_ms or manifest['end_ms'] != parent.end_ms):
            raise ValueError()
        entries = manifest['frames']
        by_id = {entry['id']: entry for entry in entries}
        if len(entries) != len(frames) or set(by_id) != {str(frame.id) for frame in frames}:
            raise ValueError()
        anchors = []
        for frame in frames:
            entry = by_id[str(frame.id)]
            pts, base = entry['pts'], Fraction(entry['time_base'])
            if (type(pts) is not int or pts < 0 or not 0 < base <= 1
                    or round(pts * base * 1000000) // 1000 != frame.timestamp_ms
                    or entry['timestamp_ms'] != frame.timestamp_ms
                    or entry['sha256'] != frame.request_sha256):
                raise ValueError()
            if window.start_ms <= pts * base * 1000 < window.end_ms:
                anchors.append({'pts': pts, 'time_base': str(base)})
        return anchors
    except (KeyError, TypeError, ValueError, ZeroDivisionError, AttributeError):
        raise ProcessingFailure('VISUAL_PARENT_EVIDENCE_INVALID', message) from None


def uploaded_frames(job, session, context, window=None):
    saved = list(session.frames.filter(id__in=window.frame_ids).order_by('timestamp_ms')) if window else list(session.frames.all())
    if saved:
        count(context, 'reused_frame_batches')
        return saved
    context.stage('extracting_visual_frames')
    start_ms, end_ms = (window.start_ms, window.end_ms) if window else (session.window_start_ms, session.window_end_ms)
    saved_paths = []
    try:
        anchors = required_parent_frames(session, window)
        with measured(context, 'frame_extraction'):
            batch = extract_video_frames(session.asset, context, start_ms=start_ms, end_ms=end_ms, count=12,
                                         **({'required_frames': anchors} if anchors else {}))
        with batch:
            samples = list(batch.frames)
            if len(samples) < 2:
                raise ProcessingFailure('INSUFFICIENT_VISUAL_FRAMES', 'Для наблюдения действия нужны минимум два разных кадра.')
            timestamps = [frame.timestamp_ms for frame in samples]
            boundaries = [start_ms, *[(a + b) // 2 for a, b in zip(timestamps, timestamps[1:])], end_ms]
            prepared = []
            with measured(context, 'frame_decode'):
                for index, sample in enumerate(samples):
                    context.check()
                    if boundaries[index + 1] <= boundaries[index]:
                        raise ProcessingFailure('VISUAL_TIMING_INVALID', 'Кадры слишком близки для проверяемого интервала.')
                    raw = sample.path.read_bytes()
                    content, width, height = decoded_jpeg(raw, max_input_bytes=1024 * 1024)
                    prepared.append((content, width, height, hashlib.sha256(raw).hexdigest()))
            with measured(context, 'frame_persistence'), transaction.atomic():
                Organization.objects.select_for_update().get(pk=job.organization_id)
                context.commit_guard()
                current = active_session(job, lock=True)
                if window:
                    window.refresh_from_db()
                existing = list(current.frames.filter(id__in=window.frame_ids).order_by('timestamp_ms')) if window else list(current.frames.all())
                if existing:
                    return existing
                frames = []
                base_sequence = current.frames.aggregate(value=Max('sequence'))['value'] or 0
                for index, sample in enumerate(samples):
                    content, width, height, digest = prepared[index]
                    frame = persist_frame(current, sequence=base_sequence + index + 1, timestamp_ms=sample.timestamp_ms, coverage_start_ms=boundaries[index], coverage_end_ms=boundaries[index + 1], content=content, width=width, height=height, idempotency_key=f'window-{window.id}-{index + 1}' if window else f'upload-{index + 1}', request_sha256=digest, frame_id=uuid.UUID(str(sample.id)))
                    saved_paths.append(safe_frame_path(frame.storage_key))
                    frames.append(frame)
                if window:
                    window.frame_ids = [str(frame.id) for frame in frames]
                    window.source_manifest = batch.manifest()
                    window.save(update_fields=['frame_ids', 'source_manifest', 'updated_at'])
                else:
                    current.source_manifest = batch.manifest()
                    current.save(update_fields=['source_manifest', 'updated_at'])
                return frames
    except Exception:
        for path in saved_paths:
            path.unlink(missing_ok=True)
        raise


def select_live_frames(job, session):
    ids = job.payload.get('frame_ids')
    if not isinstance(ids, list) or not 2 <= len(ids) <= 12 or len(set(ids)) != len(ids):
        raise ProcessingFailure('VISUAL_EVIDENCE_INVALID', 'Неверная выборка кадров камеры.')
    try:
        frames = list(FrameEvidence.objects.filter(session=session, organization_id=job.organization_id, id__in=ids).order_by('sequence'))
    except (ValueError, TypeError):
        raise ProcessingFailure('VISUAL_EVIDENCE_INVALID', 'Неверные идентификаторы кадров камеры.') from None
    if [str(frame.id) for frame in frames] != ids:
        raise ProcessingFailure('VISUAL_EVIDENCE_INVALID', 'Кадры недоступны или не относятся к этому сеансу.')
    return frames


def readable_frames(frames):
    result = []
    for frame in frames:
        path = safe_frame_path(frame.storage_key)
        if not path.is_file() or path.stat().st_size != frame.size_bytes or frame.size_bytes > 524288:
            raise ProcessingFailure('VISUAL_FRAME_UNAVAILABLE', 'Один из исходных кадров недоступен.')
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != frame.sha256:
            raise ProcessingFailure('VISUAL_FRAME_CHANGED', 'Проверка целостности исходного кадра не пройдена.')
        result.append({'id': str(frame.id), 'sequence': frame.sequence, 'timestamp_ms': frame.timestamp_ms, 'bytes': content, 'sha256': frame.sha256})
    return result


def process_job(job, context):
    try:
        return _process_job(job, context)
    except DomainError as error:
        raise ProcessingFailure(error.code, error.message) from None


def _process_job(job, context):
    context.check()
    session = active_session(job, allow_completed=True)
    existing = VisualProposal.objects.filter(source_job_id=job.id, session=session, organization_id=job.organization_id).first()
    if existing:
        check_grounding_checkpoint(existing.content, session)
        return {'visual_session_id': str(session.id), 'visual_proposal_id': str(existing.id)}
    if session.state != 'active':
        raise ProcessingFailure('VISUAL_SESSION_CLOSED', 'Сеанс анализа завершён.')
    if session.mode == 'upload' and session.analysis_windows.exists():
        return process_windows(job, session, context)
    frames = select_live_frames(job, session) if session.mode == 'live' else uploaded_frames(job, session, context)
    with measured(context, 'frame_reading'):
        images = readable_frames(frames)
    count(context, 'prepared_frames', len(images))
    context.stage('analyzing_visual_frames')
    # Recheck consent, membership, session lifetime and lease immediately before
    # starting a cloud call. Retries consume this same bounded call allowance.
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=job.organization_id)
        context.commit_guard()
        session = active_session(job, lock=True)
        if session.mode == 'live' and (session.analysis_calls >= 60 or (session.last_call_at and (timezone.now() - session.last_call_at).total_seconds() < 8)):
            raise ProcessingFailure('VISUAL_CALL_LIMIT', 'Лимит или интервал обращений камеры к ИИ достигнут.')
        session.analysis_calls += 1
        session.last_call_at = timezone.now()
        session.save(update_fields=['analysis_calls', 'last_call_at', 'updated_at'])
    try:
        provider = provider_for_job(job, session, images=images)
        with measured(context, 'model_request'):
            result = execute_budgeted(provider, job=job, session=session, frames=images, context=context, call_key=f'live:{job.id}' if session.mode == 'live' else f'legacy:{job.id}')
    except ProviderFailure as error:
        raise ProcessingFailure(error.code, error.message, retryable=error.retryable) from None
    context.check()
    context.stage('validating_visual_proposal')
    try:
        with measured(context, 'validation'):
            content = validate(result, frames, session, expected_version=protocol_for_job(job, session),
                               expected_coverage=session.analysis_budget.get('short_action_coverage'),
                               expected_grounding=session.analysis_budget.get('frame_grounding'))
    except InvalidModelOutput as error:
        # Only known codes/messages leave this boundary, never model text or IDs.
        messages = {
            'VISUAL_LANGUAGE_MISMATCH': 'ИИ вернул предложение на другом языке. Рабочие шаги сохранены.',
            'VISUAL_EVIDENCE_UNKNOWN': 'ИИ сослался на кадр вне выбранной записи. Рабочие шаги сохранены.',
            'VISUAL_EVIDENCE_ORDER_INVALID': 'ИИ пропустил или переставил кадры внутри действия. Рабочие шаги сохранены.',
        }
        code = str(error)
        if code in messages:
            raise ProcessingFailure(code, messages[code]) from None
        raise ProcessingFailure('VISUAL_OUTPUT_INVALID', 'Предложение не прошло проверку кадров и структуры. Рабочие шаги сохранены.') from None
    except (TypeError, ValueError, RecursionError):
        raise ProcessingFailure('VISUAL_OUTPUT_INVALID', 'Предложение не прошло проверку кадров и структуры. Рабочие шаги сохранены.') from None
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=job.organization_id)
        context.commit_guard()
        session = active_session(job, lock=True)
        proposal, _ = VisualProposal.objects.get_or_create(source_job_id=job.id, defaults={'organization_id': job.organization_id, 'session': session, 'provider': provider.provider, 'model': provider.model, 'content': content})
        if session.mode == 'upload':
            session.state = 'completed'
            session.save(update_fields=['state', 'updated_at'])
        return {'visual_session_id': str(session.id), 'visual_proposal_id': str(proposal.id)}


def publish_progress(job, session, context):
    with transaction.atomic():
        current_job = context.commit_guard()
        current_job.result = with_metrics({'visual_session_id': str(session.id), 'analysis': analysis_data(session)}, context)
        current_job.save(update_fields=['result', 'updated_at'])


def run_window(job, session, window, context, provider):
    """Never repeat a possibly dispatched window after a worker interruption."""
    if window.state == 'completed':
        if window.content.get('schema_version') != protocol_for_job(job, session):
            raise ProcessingFailure('VISUAL_PROTOCOL_INVALID', 'Сохранённый фрагмент относится к другому режиму анализа. Начните новый сеанс.')
        if window.content.get('temporal_coverage', {}).get('version') != session.analysis_budget.get('short_action_coverage'):
            raise ProcessingFailure('VISUAL_PROTOCOL_INVALID', 'Сохранённый фрагмент использует другой режим разбора переходов.')
        check_grounding_checkpoint(window.content, session, window=True)
        return
    if window.state == 'running':
        # The receipt may still settle later. A new attempt cannot infer that an
        # interrupted network request was free or that it was never delivered.
        window.state, window.error_code = 'failed', 'AI_CALL_OUTCOME_UNKNOWN'
        window.save(update_fields=['state', 'error_code', 'updated_at'])
        return
    if window.state == 'failed':
        return
    frames = uploaded_frames(job, session, context, window)
    with measured(context, 'frame_reading'):
        images = readable_frames(frames)
    count(context, 'prepared_frames', len(images))
    context.stage('refining_visual_window' if window.parent_id else 'analyzing_video_window')
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=job.organization_id)
        context.commit_guard()
        session = active_session(job, lock=True)
        window = VisualAnalysisWindow.objects.select_for_update().get(pk=window.id, session=session)
        if window.state != 'pending':
            return
        window.state = 'running'
        window.save(update_fields=['state', 'updated_at'])
        session.analysis_calls += 1
        session.last_call_at = timezone.now()
        session.save(update_fields=['analysis_calls', 'last_call_at', 'updated_at'])
    try:
        # A timed-out HTTP call can still deliver a late receipt. Never reuse
        # its provider instance/callback for the following paid request.
        current_provider = provider_for_job(job, session, images=images)
        with measured(context, 'model_request'):
            value = execute_budgeted(current_provider, job=job, session=session, frames=images, context=context, call_key=f'window:{window.id}')
        context.check()
        with measured(context, 'validation'):
            content = validate(value, frames, session, expected_version=protocol_for_job(job, session),
                               expected_coverage=session.analysis_budget.get('short_action_coverage'),
                               expected_grounding=session.analysis_budget.get('frame_grounding'))
    except ProviderFailure as error:
        with transaction.atomic():
            context.commit_guard()
            VisualAnalysisWindow.objects.filter(pk=window.id, state='running').update(state='failed', error_code=error.code[:64])
        raise
    except (InvalidModelOutput, TypeError, ValueError, RecursionError):
        with transaction.atomic():
            context.commit_guard()
            VisualAnalysisWindow.objects.filter(pk=window.id, state='running').update(state='failed', error_code='VISUAL_OUTPUT_INVALID')
        return
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=job.organization_id)
        context.commit_guard()
        active_session(job, lock=True)
        VisualAnalysisWindow.objects.filter(pk=window.id, state='running').update(state='completed', content=content, error_code='')
    count(context, 'completed_windows')


def process_windows(job, session, context):
    from .review_worker import enabled as review_enabled, effective_content, run_review
    provider = provider_for_job(job, session)
    checking = review_enabled(session)
    stop_code = ''
    overview = list(session.analysis_windows.filter(parent__isnull=True))
    # Persist a checkpoint after each window; all overview windows have priority
    # over optional refinement, so difficult early footage cannot starve the end.
    for window in overview:
        context.check()
        active_session(job)
        try:
            run_window(job, session, window, context, provider)
        except ProviderFailure as error:
            stop_code = error.code
            if error.code.startswith('AI_'):
                publish_progress(job, session, context)
                break
        publish_progress(job, session, context)
    # Finish the full overview before spending optional calls. Check confident
    # claims too: hallucinated actions often arrive without uncertainty flags.
    if checking and (not stop_code or not stop_code.startswith('AI_')):
        for window in session.analysis_windows.filter(parent__isnull=True, state='completed'):
            code = run_review(job, session, window, context)
            publish_progress(job, session, context)
            if code:
                stop_code = code
            if code.startswith('AI_'):
                break
    # Grounding adds local consistency checks to the existing response. A new
    # gap/unknown must never spend another call merely because it was exposed.
    if session.analysis_budget.get('frame_grounding') is None and (not stop_code or not stop_code.startswith('AI_')):
        for parent in session.analysis_windows.filter(parent__isnull=True, state='completed').select_related('semantic_review'):
            if not needs_refinement(effective_content(parent)):
                continue
            children = list(parent.refinements.all())
            if not children:
                parts = intervals(parent.start_ms, parent.end_ms, REFINEMENT_WINDOW_MS)
                # A very short window still gets denser temporal evidence by
                # splitting once; never recursively refine the resulting parts.
                if len(parts) == 1 and parent.end_ms - parent.start_ms >= 2000:
                    midpoint = (parent.start_ms + parent.end_ms) // 2
                    parts = [(parent.start_ms, midpoint), (midpoint, parent.end_ms)]
                if parent.end_ms - parent.start_ms < 2000:
                    continue
                try:
                    # New denser candidates also need checking in this mode.
                    # Do not spend all remaining calls on unchecked refinements.
                    preflight(session, len(parts) * (2 if checking else 1), provider=provider)
                except ProviderFailure as error:
                    stop_code = error.code
                    break
                with transaction.atomic():
                    Organization.objects.select_for_update().get(pk=job.organization_id)
                    context.commit_guard()
                    active_session(job, lock=True)
                    last = session.analysis_windows.aggregate(value=Max('ordinal'))['value'] or 0
                    children = [VisualAnalysisWindow.objects.create(organization_id=session.organization_id, session=session, parent=parent, ordinal=last + index + 1, start_ms=start, end_ms=end) for index, (start, end) in enumerate(parts)]
            for child in children:
                context.check()
                try:
                    run_window(job, session, child, context, provider)
                    if checking:
                        child.refresh_from_db()
                        code = run_review(job, session, child, context)
                        if code:
                            stop_code = code
                except ProviderFailure as error:
                    stop_code = error.code
                    if error.code.startswith('AI_'):
                        break
                publish_progress(job, session, context)
                if stop_code.startswith('AI_'):
                    break
            if stop_code.startswith('AI_'):
                break
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=job.organization_id)
        context.commit_guard()
        session = active_session(job, lock=True)
        windows = list(session.analysis_windows.select_related('semantic_review'))
        with measured(context, 'aggregation'):
            content = aggregate_content(session, windows, stop_code=stop_code)
        proposal, _ = VisualProposal.objects.get_or_create(source_job_id=job.id, defaults={'organization_id': job.organization_id, 'session': session, 'provider': provider.provider, 'model': provider.model, 'content': content})
        session.state = 'completed'
        session.source_manifest = {'sampling': 'discrete_frames', 'sparse_sampling': True, 'windows': [{'window_id': str(window.id), **window.source_manifest} for window in windows if window.source_manifest]}
        session.save(update_fields=['state', 'source_manifest', 'updated_at'])
        return {'visual_session_id': str(session.id), 'visual_proposal_id': str(proposal.id), 'analysis': content['analysis']}
