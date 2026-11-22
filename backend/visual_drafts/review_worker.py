"""One optional, separately accounted visual check; never a hidden retry."""
from copy import deepcopy

from django.db import transaction
from django.utils import timezone

from ai_budget.services import execute_budgeted, preflight
from ai_drafts.providers import ProviderFailure
from ai_drafts.validation import InvalidModelOutput
from core.models import Organization
from media_pipeline.exceptions import ProcessingFailure
from media_pipeline.telemetry import count, measured
from .models import VisualAnalysisWindow, VisualAnalysisReview
from .pair_verification import VERSION as PAIR_REVIEW_VERSION, PairVerificationError


REVIEW_VERSION = 'v1'


def enabled(session):
    budget = getattr(session, 'analysis_budget', {})
    if getattr(session, 'mode', None) != 'upload' or budget.get('visual_protocol') != '3':
        return False
    version = budget.get('semantic_review')
    if version == REVIEW_VERSION:
        return True
    if version == PAIR_REVIEW_VERSION:
        from .demonstrations import SHARED_CAPTIONS_WIRE_VERSION
        return (budget.get('frame_grounding') == 'v3'
                and budget.get('short_action_coverage') == 'v1'
                and budget.get('demonstration_wire') == SHARED_CAPTIONS_WIRE_VERSION)
    return False


def review_eligible(window, version):
    return window.state == 'completed' and (version != PAIR_REVIEW_VERSION or bool(window.content.get('steps')))


def review_state(window, version=None):
    review = getattr(window, 'semantic_review', None)
    if review and (version != PAIR_REVIEW_VERSION or review.version == version):
        return review.state
    return None


def effective_content(window, version=None):
    return window.semantic_review.content if review_state(window, version) == 'completed' else window.content


def run_review(job, session, window, context):
    """Keep the original on failure, retain both versions on success.

    The running checkpoint precedes dispatch. An interrupted check is never
    replayed, even if the job later acquires a new lease. Every model call goes
    through the same session/org/platform ledger as ordinary video analysis.
    """
    from . import worker

    if not enabled(session):
        return ''
    version = session.analysis_budget['semantic_review']
    if not review_eligible(window, version):
        return ''
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=job.organization_id)
        context.commit_guard()
        worker.active_session(job, lock=True)
        current = VisualAnalysisWindow.objects.select_for_update().get(pk=window.id, session=session)
        record = VisualAnalysisReview.objects.select_for_update().filter(window=current).first()
        if record:
            if record.version != version:
                return 'VISUAL_REVIEW_CHECKPOINT_CHANGED'
            if record.state == 'running':
                record.state, record.error_code = 'failed', 'AI_CALL_OUTCOME_UNKNOWN'
                record.save(update_fields=['state', 'error_code', 'updated_at'])
            return record.error_code
        original = deepcopy(current.content)

    try:
        frames = list(session.frames.filter(id__in=current.frame_ids).order_by('timestamp_ms'))
        images = worker.readable_frames(frames)
        if version == PAIR_REVIEW_VERSION:
            from .pair_verification import VisualPairReviewProvider, candidate_from_content
            provider = VisualPairReviewProvider(session.organization, candidate=candidate_from_content(original),
                                                model=session.analysis_budget.get('semantic_review_model'))
        else:
            from .review import VisualReviewProvider, candidate_from_content
            provider = VisualReviewProvider(session.organization, candidate=candidate_from_content(original))
        # Pure local bounds/reference checks run before either checkpoint or spend.
        provider.prepare_request(language=session.language, mode=session.mode, frames=images)
        preflight(session, 1, provider=provider)
    except (ProviderFailure, InvalidModelOutput, ValueError, TypeError) as error:
        code = error.code if isinstance(error, ProviderFailure) else 'VISUAL_REVIEW_INPUT_INVALID'
        _record_failure(job, session, current, context, code, state='skipped')
        return code

    context.stage('checking_visual_actions')
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=job.organization_id)
        context.commit_guard()
        active = worker.active_session(job, lock=True)
        current = VisualAnalysisWindow.objects.select_for_update().get(pk=window.id, session=session)
        # A different worker must never replace the candidate after preparation.
        if VisualAnalysisReview.objects.filter(window=current).exists() or current.content != original:
            return 'VISUAL_REVIEW_CHECKPOINT_CHANGED'
        record = VisualAnalysisReview.objects.create(organization_id=job.organization_id, window=current,
                                                    version=version, state='running')
        active.analysis_calls += 1
        active.last_call_at = timezone.now()
        active.save(update_fields=['analysis_calls', 'last_call_at', 'updated_at'])
    try:
        with measured(context, 'visual_review_request'):
            result = execute_budgeted(provider, job=job, session=session, frames=images,
                                      context=context, call_key=f'verify:{window.id}')
        context.check()
        if version == PAIR_REVIEW_VERSION:
            from .pair_verification import apply_verdicts
            content = apply_verdicts(result, original, frames, session)
        else:
            content = worker.validate(result, frames, session, expected_version='3')
    except ProviderFailure as error:
        _record_failure(job, session, current, context, error.code)
        return error.code
    except ProcessingFailure as error:
        if version != PAIR_REVIEW_VERSION or error.code != 'PROVIDER_TIMEOUT':
            raise
        _record_failure(job, session, current, context, error.code)
        return error.code
    except (InvalidModelOutput, TypeError, ValueError, RecursionError) as error:
        diagnostic = error.diagnostic if isinstance(error, PairVerificationError) else None
        _record_failure(job, session, current, context, 'VISUAL_REVIEW_OUTPUT_INVALID', diagnostic=diagnostic)
        return 'VISUAL_REVIEW_OUTPUT_INVALID'
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=job.organization_id)
        context.commit_guard()
        worker.active_session(job, lock=True)
        record = VisualAnalysisReview.objects.select_for_update().get(window_id=window.id, organization_id=job.organization_id)
        if record.state != 'running':
            return 'VISUAL_REVIEW_CHECKPOINT_CHANGED'
        # An empty corrected answer can legitimately reject all candidate actions.
        # Never resurrect them just because the first answer had more steps.
        record.content, record.state, record.error_code = content, 'completed', ''
        record.save(update_fields=['content', 'state', 'error_code', 'updated_at'])
    count(context, 'completed_visual_reviews')
    return ''


def _record_failure(job, session, window, context, code, *, state='failed', diagnostic=None):
    from .worker import active_session
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=job.organization_id)
        context.commit_guard()
        active_session(job, lock=True)
        version = session.analysis_budget['semantic_review']
        record, _ = VisualAnalysisReview.objects.select_for_update().get_or_create(
            window_id=window.id, defaults={'organization_id': job.organization_id, 'version': version})
        if record.state != 'running' or record.version != version:
            return
        record.state, record.error_code = state, code[:64]
        if diagnostic:
            record.content = {'validation_diagnostic': diagnostic}
        record.save(update_fields=['state', 'error_code', 'content', 'updated_at'])
