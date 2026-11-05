"""Full-video scheduling tests use real accounting and deterministic model responses."""
import hashlib
from types import SimpleNamespace

import pytest
from django.db import IntegrityError, transaction

from ai_drafts.providers import ProviderFailure
from ai_budget.models import AIUsageReservation
from core.models import Instruction, MediaAsset, WorkingCopy
from core.services import etag
from media_pipeline.models import ProcessingJob
from media_pipeline.worker import process_one
from visual_drafts import worker, services
from visual_drafts.models import VisualAnalysisWindow, VisualProposal, VisualSession
from visual_drafts.planning import intervals
from visual_drafts.provider import validate
from .conftest import post
from .test_visual_drafts import jpeg

pytestmark = pytest.mark.django_db


@pytest.fixture
def full_video(team, settings):
    settings.STEP_DRAFT_BACKEND = 'openai'
    settings.STEP_DRAFT_ENDPOINT = 'http://127.0.0.1:12345/v1'
    settings.STEP_DRAFT_MODEL = 'gpt-5.4-mini'
    settings.STEP_DRAFT_API_KEY = ''
    settings.EXTERNAL_AI_ENABLED = False
    asset = MediaAsset.objects.create(organization=team.org, storage_key='test-video.mp4', duration_ms=70000, status='ready', has_audio=False)
    instruction = Instruction.objects.create(organization=team.org, owner=team.users['author'], source_asset=asset)
    working = WorkingCopy.objects.create(organization=team.org, instruction=instruction, title='Проверка полного видео', purpose='Сохранить все интервалы', language='ru')
    return SimpleNamespace(asset=asset, instruction=instruction, working=working, url=f'/api/v1/orgs/{team.org.id}/instructions/{instruction.id}')


def queue(team, source, **extra):
    response = post(team.clients['author'], source.url + '/visual-proposals', {'consent_images': True, 'scope': 'full', **extra}, etag=etag(source.working), key='full')
    assert response.status_code == 202, response.json()
    return ProcessingJob.objects.get(pk=response.json()['id']), VisualSession.objects.get(pk=response.json()['result']['visual_session_id'])


def fake_extraction(job, session, context, window=None):
    if window.frame_ids:
        return list(session.frames.filter(id__in=window.frame_ids).order_by('timestamp_ms'))
    midpoint = (window.start_ms + window.end_ms) // 2
    bounds = [window.start_ms, midpoint, window.end_ms]
    sequence = session.frames.count()
    frames = []
    for index in range(2):
        raw = jpeg('red' if index else 'blue')
        frames.append(services.persist_frame(session, sequence=sequence + index + 1, timestamp_ms=bounds[index], coverage_start_ms=bounds[index], coverage_end_ms=bounds[index + 1], content=raw, width=80, height=60, idempotency_key=f'{window.id}:{index}', request_sha256=hashlib.sha256(raw).hexdigest()))
    window.frame_ids = [str(frame.id) for frame in frames]
    window.source_manifest = {'sparse_sampling': True, 'start_ms': window.start_ms, 'end_ms': window.end_ms}
    window.save(update_fields=['frame_ids', 'source_manifest'])
    return frames


def install_model(monkeypatch, *, uncertainty=False, fail_start=None, steps_per_window=1, refinement_empty=False):
    calls = []
    class Fake:
        provider, model = 'openai', 'gpt-5.4-mini'
        endpoint = 'https://api.openai.com/v1'
        def __init__(self, organization):
            pass
        def generate(self, *, language, mode, frames, context=None):
            start = frames[0]['timestamp_ms']
            calls.append(start)
            if start == fail_start:
                raise ProviderFailure('MODEL_OUTPUT_INVALID', 'Некорректный ответ.')
            value = {'schema_version': '2', 'language': language, 'steps': [{'title': f'Наблюдение {index}', 'description': 'Видимое изменение предмета между двумя кадрами.', 'evidence_frame_ids': [frame['id'] for frame in frames]} for index in range(steps_per_window)], 'warnings': []}
            if uncertainty and start == 0 and frames[-1]['timestamp_ms'] >= 15000:
                value['warnings'] = [{'code': 'uncertain_action', 'message': 'Положение руки закрывает начало действия.'}]
            if refinement_empty and (start == 15000 or (start == 0 and frames[-1]['timestamp_ms'] == 7500)):
                value['steps'] = []
            # Provider receipts are optional here: missing receipts intentionally
            # hold the full cost reservation while call caps remain authoritative.
            return value
    monkeypatch.setattr(worker, 'VisualProvider', Fake)
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
    return calls


def test_temporal_plan_covers_tail_without_gaps():
    assert intervals(0, 70000) == [(0, 30000), (30000, 60000), (60000, 70000)]
    assert intervals(0, 600000)[-1] == (570000, 600000)
    assert len(intervals(0, 600000)) == 20


def test_full_video_finishes_overview_before_refining_uncertain_window(team, full_video, monkeypatch):
    calls = install_model(monkeypatch, uncertainty=True)
    job, session = queue(team, full_video, max_ai_calls=5)
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done', (job.error_code, job.error_summary)
    assert calls == [0, 30000, 60000, 0, 15000]
    analysis = job.result['analysis']
    assert analysis['coverage_complete'] and analysis['windows_completed'] == 3
    assert analysis['refinements_completed'] == 2 and not analysis['unprocessed_ranges']
    assert analysis['budget']['calls_used'] == 5
    assert AIUsageReservation.objects.count() == 5
    assert len(VisualProposal.objects.get().content['steps']) == 4
    assert not full_video.working.steps.exists()


def test_budget_preserves_full_overview_and_stops_extra_calls(team, full_video, monkeypatch):
    calls = install_model(monkeypatch, uncertainty=True)
    job, session = queue(team, full_video, max_ai_calls=3)
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done' and calls == [0, 30000, 60000]
    proposal = VisualProposal.objects.get()
    assert proposal.content['analysis']['coverage_complete']
    assert 'refinement_incomplete' in {warning['code'] for warning in proposal.content['warnings']}
    assert not session.analysis_windows.filter(parent__isnull=False).exists()
    assert AIUsageReservation.objects.count() == 3


def test_partial_result_names_unprocessed_interval_and_keeps_other_windows(team, full_video, monkeypatch):
    calls = install_model(monkeypatch, fail_start=30000)
    job, session = queue(team, full_video, max_ai_calls=3)
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done' and calls == [0, 30000, 60000]
    analysis = job.result['analysis']
    assert not analysis['coverage_complete'] and analysis['windows_completed'] == 2
    assert analysis['unprocessed_ranges'] == [{'start_ms': 30000, 'end_ms': 60000}]
    assert len(VisualProposal.objects.get().content['steps']) == 2


@pytest.mark.parametrize('budget,code', [({'max_ai_calls': 2}, 'AI_BUDGET_TOO_SMALL'), ({'max_cost_microusd': 0}, 'AI_BUDGET_EXCEEDED')])
def test_preflight_rejects_insufficient_baseline_before_creating_job(team, full_video, budget, code):
    response = post(team.clients['author'], full_video.url + '/visual-proposals', {'consent_images': True, 'scope': 'full', **budget}, etag=etag(full_video.working), key='insufficient')
    assert response.status_code == 422 and response.json()['error']['code'] == code
    assert not ProcessingJob.objects.exists() and not VisualSession.objects.exists()
    assert not AIUsageReservation.objects.exists()


def test_resume_skips_checkpoint_and_never_repeats_uncertain_dispatched_window(team, full_video, monkeypatch):
    calls = install_model(monkeypatch)
    job, session = queue(team, full_video, max_ai_calls=3)
    windows = list(session.analysis_windows.all())
    frames = fake_extraction(job, session, None, windows[0])
    result = {'schema_version': '2', 'language': 'ru', 'steps': [{'title': 'Сохранённое наблюдение', 'description': 'Это ранее проверенный ответ.', 'evidence_frame_ids': [str(frame.id) for frame in frames]}], 'warnings': []}
    windows[0].state, windows[0].content = 'completed', validate(result, frames, session)
    windows[0].save()
    windows[1].state = 'running'
    windows[1].save()
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done' and calls == [60000]
    windows[1].refresh_from_db()
    assert windows[1].state == 'failed' and windows[1].error_code == 'AI_CALL_OUTCOME_UNKNOWN'
    assert job.result['analysis']['windows_completed'] == 2
    assert len(VisualProposal.objects.get().content['steps']) == 2


def test_more_than_thirty_steps_are_retained_but_cannot_apply(team, full_video, monkeypatch):
    install_model(monkeypatch, steps_per_window=16)
    job, session = queue(team, full_video, max_ai_calls=3)
    assert process_one()
    proposal = VisualProposal.objects.get()
    assert len(proposal.content['steps']) == 48
    response = team.clients['author'].get(full_video.url + f'/visual-proposals/{proposal.id}')
    assert response.status_code == 200 and not response.json()['can_apply']
    assert 'instruction_step_limit' in {w['code'] for w in response.json()['warnings']}
    applied = post(team.clients['author'], full_video.url + f'/visual-proposals/{proposal.id}/apply', {'confirm_replace': True}, etag=etag(full_video.working), key='apply-large')
    assert applied.status_code == 422 and not full_video.working.steps.exists()


def test_window_tenant_and_parent_boundaries_enforced_by_database(team, full_video):
    job, session = queue(team, full_video)
    parent = session.analysis_windows.first()
    with pytest.raises(IntegrityError), transaction.atomic():
        VisualAnalysisWindow.objects.create(organization=team.other, session=session, ordinal=99, start_ms=0, end_ms=1000)
    with pytest.raises(IntegrityError), transaction.atomic():
        VisualAnalysisWindow.objects.create(organization=team.org, session=session, parent=parent, ordinal=99, start_ms=0, end_ms=parent.end_ms + 1)


def test_completed_checkpoint_and_session_budget_are_immutable(team, full_video, monkeypatch):
    install_model(monkeypatch)
    job, session = queue(team, full_video, max_ai_calls=3)
    assert process_one()
    first = session.analysis_windows.first()
    with pytest.raises(IntegrityError), transaction.atomic():
        VisualAnalysisWindow.objects.filter(pk=first.pk).update(content={})
    with pytest.raises(IntegrityError), transaction.atomic():
        VisualSession.objects.filter(pk=session.pk).update(analysis_budget={'max_calls': 10000})


def test_unhelpful_refinement_keeps_valid_overview_and_discloses_uncertainty(team, full_video, monkeypatch):
    install_model(monkeypatch, uncertainty=True, refinement_empty=True)
    job, session = queue(team, full_video, max_ai_calls=5)
    assert process_one()
    proposal = VisualProposal.objects.get()
    assert len(proposal.content['steps']) == 3
    assert proposal.content['steps'][0]['end_ms'] == 30000
    assert 'refinement_unresolved' in {w['code'] for w in proposal.content['warnings']}
    assert session.analysis_windows.filter(parent__isnull=False, state='completed').count() == 2
    assert all(not child.content['steps'] for child in session.analysis_windows.filter(parent__isnull=False))


def test_proposal_and_job_budget_refresh_after_late_usage_receipt(team, full_video, monkeypatch):
    from ai_budget.services import settle
    from media_pipeline.jobs import job_data
    install_model(monkeypatch)
    job, session = queue(team, full_video, max_ai_calls=3)
    assert process_one()
    proposal = VisualProposal.objects.get()
    old_content = proposal.content.copy()
    before = services.proposal_data(proposal)['analysis']['budget']
    reservation = AIUsageReservation.objects.filter(session_id=session.id).first()
    assert reservation.state == 'unknown'
    settle(reservation, {'prompt_tokens': 1000, 'completion_tokens': 100}, 'late-receipt')
    after = services.proposal_data(proposal)['analysis']['budget']
    assert after['spent_microusd'] > before['spent_microusd']
    assert after['reserved_microusd'] < before['reserved_microusd']
    assert after['unknown_calls'] == before['unknown_calls'] - 1
    job.refresh_from_db()
    assert job_data(job)['result']['analysis']['budget'] == after
    proposal.refresh_from_db()
    assert proposal.content == old_content
