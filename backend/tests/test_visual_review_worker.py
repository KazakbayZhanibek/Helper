"""Review scheduling with real reservations and deterministic model substitutes."""
from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest
from django.db import IntegrityError, transaction

from ai_budget.models import AIUsageReservation, BudgetAccount
from ai_drafts import providers
from ai_drafts.providers import ProviderFailure
from core.services import etag
from media_pipeline.models import ProcessingJob
from media_pipeline.worker import process_one
from visual_drafts import planning, provider, review, services, worker
from visual_drafts.models import VisualAnalysisReview, VisualAnalysisWindow, VisualProposal, VisualSession
from .conftest import post
from .test_visual_drafts import add_frames
from .test_visual_full_video import full_video, fake_extraction, queue

pytestmark = pytest.mark.django_db


@pytest.fixture
def reviewed_video(full_video, settings, monkeypatch):
    settings.VISUAL_ACTION_EVIDENCE = True
    settings.VISUAL_SEMANTIC_REVIEW = True
    config = lambda org: {'available': True, 'provider': 'codex', 'model': 'gpt-6-luna'}
    monkeypatch.setattr(services, 'capabilities', config)
    monkeypatch.setattr(worker, 'capabilities', config)
    monkeypatch.setattr(providers, 'endpoint_configuration', config)
    return full_video


def wire(frames, *, checked=False, gap=False, empty=False, language='ru'):
    refs = [str(frame['id']) for frame in frames]
    result = {
        'schema_version': '3', 'language': language,
        'observations': [] if empty else [{
            'id': 'o1', 'kind': 'action', 'subject': 'Предмет',
            'title': 'Проверенное движение' if checked else 'Исходное движение',
            'before': None, 'before_frame_id': None,
            'movement': 'Удерживайте предмет и сдвиньте вправо.' if checked else 'Переместите предмет вправо.',
            'action_frame_ids': refs, 'after': None, 'after_frame_id': None, 'evidence_frame_ids': refs,
        }],
        'gaps': [], 'warnings': [],
    }
    if gap or empty:
        result['gaps'] = [{'reason': 'unclear_action', 'description': 'Способ захвата неразличим. Уточните его по записи.', 'evidence_frame_ids': refs}]
    return result


def install_models(monkeypatch, *, review_mode='normal', uncertain_overview=False, fail_child_review=False):
    calls, candidates = [], []

    class Original:
        provider, model, protocol = 'codex', 'gpt-6-luna', '3'
        endpoint = ''

        def __init__(self, organization):
            pass

        def prepare_request(self, **kwargs):
            return None

        def generate(self, *, language, mode, frames, context=None):
            calls.append(('original', frames[0]['timestamp_ms'], frames[-1]['timestamp_ms']))
            if self.protocol == '2':
                return {'schema_version': '2', 'language': language, 'steps': [{'title': 'Исходное движение', 'description': 'Передвиньте предмет.', 'evidence_frame_ids': [frame['id'] for frame in frames]}], 'warnings': []}
            overview = frames[-1]['timestamp_ms'] - frames[0]['timestamp_ms'] >= 10000
            return wire(frames, language=language, gap=uncertain_overview and overview)

    class Reviewer(Original):
        def __init__(self, organization, *, candidate):
            self.candidate = deepcopy(candidate)
            candidates.append(self.candidate)

        def prepare_request(self, *, language, mode, frames):
            return review.request_review_contract(language, mode, frames, self.candidate)

        def generate(self, *, language, mode, frames, context=None):
            calls.append(('review', frames[0]['timestamp_ms'], frames[-1]['timestamp_ms']))
            overview = frames[-1]['timestamp_ms'] - frames[0]['timestamp_ms'] >= 10000
            if review_mode == 'failure' or (fail_child_review and not overview):
                raise ProviderFailure('REVIEW_FAILED', 'Проверка не завершилась.')
            result = wire(frames, checked=True, empty=review_mode == 'empty', language=language,
                          gap=uncertain_overview and overview)
            if review_mode == 'foreign':
                result['observations'][0]['action_frame_ids'][-1] = str(uuid.uuid4())
            return result

    monkeypatch.setattr(worker, 'VisualProvider', Original)
    monkeypatch.setattr(review, 'VisualReviewProvider', Reviewer)
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
    return calls, candidates


def one_window(team, source, *, cap=2):
    return queue(team, source, scope='window', start_ms=0, end_ms=20000, max_ai_calls=cap)


def finish(job):
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done', (job.error_code, job.error_summary)
    return VisualProposal.objects.get(source_job_id=job.id)


def test_original_window_remains_immutable_and_corrected_result_is_selected(team, reviewed_video, monkeypatch):
    calls, candidates = install_models(monkeypatch)
    job, session = one_window(team, reviewed_video)
    proposal = finish(job)
    window = session.analysis_windows.get()
    checked = VisualAnalysisReview.objects.get(window=window)
    assert window.content['steps'][0]['title'] == 'Исходное движение'
    assert candidates[0] == review.candidate_from_content(window.content)
    assert checked.state == 'completed' and checked.content['steps'][0]['title'] == 'Проверенное движение'
    assert proposal.content['steps'] == checked.content['steps']
    assert calls == [('original', 0, 10000), ('review', 0, 10000)]
    assert AIUsageReservation.objects.filter(session_id=session.id, state='settled').count() == 2
    assert set(AIUsageReservation.objects.values_list('call_key', flat=True)) == {f'window:{window.id}', f'verify:{window.id}'}
    assert BudgetAccount.objects.get(key=f'calls:session:{session.id}').spent == 2
    session.refresh_from_db()
    assert session.analysis_calls == 2
    assert not reviewed_video.working.steps.exists()
    assert 'semantic_review_completed' in {item['code'] for item in proposal.content['warnings']}
    for model, row in [(VisualAnalysisWindow, window), (VisualAnalysisReview, checked)]:
        with pytest.raises(IntegrityError), transaction.atomic():
            model.objects.filter(pk=row.pk).update(content={})


def test_completed_empty_review_rejects_candidate_instead_of_resurrecting_steps(team, reviewed_video, monkeypatch):
    install_models(monkeypatch, review_mode='empty')
    job, session = one_window(team, reviewed_video)
    proposal = finish(job)
    window = session.analysis_windows.get()
    assert window.content['steps']
    assert window.semantic_review.state == 'completed'
    assert window.semantic_review.content['steps'] == proposal.content['steps'] == []
    assert proposal.content['gaps'] == window.semantic_review.content['gaps']
    assert proposal.content['analysis']['semantic_review']['windows_checked'] == 1
    assert AIUsageReservation.objects.count() == 2


def test_single_call_cap_preserves_overview_and_skips_review_without_dispatch(team, reviewed_video, monkeypatch):
    calls, _ = install_models(monkeypatch)
    job, session = one_window(team, reviewed_video, cap=1)
    proposal = finish(job)
    record = VisualAnalysisReview.objects.get()
    assert calls == [('original', 0, 10000)]
    assert AIUsageReservation.objects.count() == 1
    assert record.state == 'skipped' and record.error_code == 'AI_BUDGET_EXCEEDED'
    assert proposal.content['steps'][0]['title'] == 'Исходное движение'
    assert 'semantic_review_incomplete' in {item['code'] for item in proposal.content['warnings']}
    assert proposal.content['analysis']['budget']['calls_used'] == 1
    session.refresh_from_db()
    assert session.analysis_calls == 1


def test_all_overview_windows_precede_any_review_and_both_consume_same_cap(team, reviewed_video, monkeypatch):
    calls, _ = install_models(monkeypatch)
    job, session = queue(team, reviewed_video, max_ai_calls=6)
    proposal = finish(job)
    assert [(kind, start) for kind, start, _ in calls] == [
        ('original', 0), ('original', 30000), ('original', 60000),
        ('review', 0), ('review', 30000), ('review', 60000),
    ]
    assert AIUsageReservation.objects.count() == 6
    assert proposal.content['analysis']['semantic_review'] == {'strategy': 'v1', 'windows_eligible': 3, 'windows_checked': 3, 'windows_unchecked': 0}
    assert BudgetAccount.objects.get(key=f'calls:session:{session.id}').spent == 6


@pytest.mark.parametrize('state', ['completed', 'running', 'failed', 'skipped'])
def test_existing_review_checkpoint_is_never_repeated_after_resume(team, reviewed_video, monkeypatch, state):
    calls, _ = install_models(monkeypatch)
    job, session = one_window(team, reviewed_video, cap=2)
    window = session.analysis_windows.get()
    frames = fake_extraction(job, session, None, window)
    images = worker.readable_frames(frames)
    original = provider.validate(wire(images), frames, session, expected_version='3')
    window.state, window.content = 'completed', original
    window.save(update_fields=['state', 'content'])
    corrected = provider.validate(wire(images, checked=True), frames, session, expected_version='3')
    record = VisualAnalysisReview.objects.create(organization=team.org, window=window, state=state,
                                                content=corrected if state == 'completed' else {},
                                                error_code='REVIEW_FAILED' if state == 'failed' else '')
    proposal = finish(job)
    record.refresh_from_db()
    assert calls == [] and not AIUsageReservation.objects.exists()
    if state == 'running':
        assert record.state == 'failed' and record.error_code == 'AI_CALL_OUTCOME_UNKNOWN'
    else:
        assert record.state == state
    assert proposal.content['steps'] == (corrected if state == 'completed' else original)['steps']


def test_foreign_reviewer_uuid_keeps_original_with_explicit_unchecked_warning(team, reviewed_video, monkeypatch):
    calls, _ = install_models(monkeypatch, review_mode='foreign')
    job, session = one_window(team, reviewed_video)
    proposal = finish(job)
    record = VisualAnalysisReview.objects.get()
    assert record.state == 'failed' and record.error_code == 'VISUAL_REVIEW_OUTPUT_INVALID'
    assert record.content == {}
    assert proposal.content['steps'] == session.analysis_windows.get().content['steps']
    assert 'semantic_review_incomplete' in {item['code'] for item in proposal.content['warnings']}
    assert len(calls) == 2 and AIUsageReservation.objects.filter(state='settled').count() == 2


@pytest.mark.parametrize('pinned', [True, False])
def test_review_strategy_is_pinned_at_session_creation_not_runtime_flag(team, reviewed_video, settings, monkeypatch, pinned):
    settings.VISUAL_SEMANTIC_REVIEW = pinned
    calls, _ = install_models(monkeypatch)
    job, session = one_window(team, reviewed_video)
    assert session.analysis_budget.get('semantic_review') == ('v1' if pinned else None)
    assert job.payload.get('semantic_review') == ('v1' if pinned else None)
    settings.VISUAL_SEMANTIC_REVIEW = not pinned
    proposal = finish(job)
    assert len(calls) == (2 if pinned else 1)
    assert VisualAnalysisReview.objects.count() == (1 if pinned else 0)
    assert ('semantic_review' in proposal.content['analysis']) is pinned


def test_legacy_upload_ignores_enabled_review_flag(team, reviewed_video, settings, monkeypatch):
    settings.VISUAL_ACTION_EVIDENCE = False
    calls, _ = install_models(monkeypatch)
    job, session = one_window(team, reviewed_video)
    proposal = finish(job)
    assert session.analysis_budget.get('semantic_review') is None
    assert proposal.content['schema_version'] == '2'
    assert len(calls) == 1 and not VisualAnalysisReview.objects.exists()


def test_live_v3_never_creates_or_runs_semantic_review(team, reviewed_video, monkeypatch):
    calls, _ = install_models(monkeypatch)
    response = post(team.clients['author'], reviewed_video.url + '/visual-sessions', {'consent_images': True}, etag=etag(reviewed_video.working), key='review-live')
    assert response.status_code == 201, response.json()
    session = VisualSession.objects.get(pk=response.json()['id'])
    add_frames(session)
    response = post(team.clients['author'], f'/api/v1/orgs/{team.org.id}/visual-sessions/{session.id}/analyze', {}, key='analyze-live')
    assert response.status_code == 202, response.json()
    job = ProcessingJob.objects.get(pk=response.json()['id'])
    proposal = finish(job)
    assert session.analysis_budget == {'visual_protocol': '3'}
    assert 'semantic_review' not in job.payload
    assert len(calls) == 1 and not VisualAnalysisReview.objects.exists()
    assert 'step_format' not in proposal.content


@pytest.mark.parametrize('cap,expected_calls,expected_children', [(5, 2, 0), (6, 6, 2)])
def test_refinement_requires_budget_for_both_candidates_and_their_reviews(team, reviewed_video, monkeypatch, cap, expected_calls, expected_children):
    calls, _ = install_models(monkeypatch, uncertain_overview=True)
    job, session = one_window(team, reviewed_video, cap=cap)
    proposal = finish(job)
    children = session.analysis_windows.filter(parent__isnull=False)
    assert children.count() == expected_children
    assert len(calls) == AIUsageReservation.objects.count() == expected_calls
    if expected_children:
        assert all(child.semantic_review.state == 'completed' for child in children)
        assert [(kind, start) for kind, start, _ in calls] == [
            ('original', 0), ('review', 0), ('original', 0), ('review', 0), ('original', 15000), ('review', 15000)]
        assert len(proposal.content['steps']) == 2
        assert all(step['title'] == 'Проверенное движение' for step in proposal.content['steps'])
    else:
        assert len(proposal.content['steps']) == 1
        assert proposal.content['steps'][0]['end_ms'] == 20000


def test_unchecked_refinement_never_replaces_checked_overview(team, reviewed_video, monkeypatch):
    calls, _ = install_models(monkeypatch, uncertain_overview=True, fail_child_review=True)
    job, session = one_window(team, reviewed_video, cap=6)
    proposal = finish(job)
    parent = session.analysis_windows.get(parent__isnull=True)
    children = session.analysis_windows.filter(parent__isnull=False)
    assert children.count() == 2
    assert all(child.state == 'completed' and child.semantic_review.state == 'failed' for child in children)
    assert proposal.content['steps'] == parent.semantic_review.content['steps']
    assert 'refinement_unresolved' in {item['code'] for item in proposal.content['warnings']}
    assert len(calls) == AIUsageReservation.objects.count() == 6


def test_review_database_guard_rejects_foreign_tenant_or_unfinished_window(team, reviewed_video):
    job, session = one_window(team, reviewed_video)
    window = session.analysis_windows.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        VisualAnalysisReview.objects.create(organization=team.org, window=window)
    frames = fake_extraction(job, session, None, window)
    window.state = 'completed'
    window.content = provider.validate(wire(worker.readable_frames(frames)), frames, session, expected_version='3')
    window.save(update_fields=['state', 'content'])
    with pytest.raises(IntegrityError), transaction.atomic():
        VisualAnalysisReview.objects.create(organization=team.other, window=window)
    assert not VisualAnalysisReview.objects.exists()
