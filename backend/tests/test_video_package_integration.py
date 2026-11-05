"""Real decoder + event selection + ledger, with a fake (never paid) model."""
import subprocess
from types import SimpleNamespace

import pytest

from ai_budget.models import AIUsageReservation
from ai_budget.services import estimate_call
from core.models import Instruction, MediaAsset, WorkingCopy
from media_pipeline.worker import process_one
from visual_drafts import worker
from visual_drafts.models import VisualProposal
from .test_media_pipeline import installed_media_binary
from .test_visual_full_video import queue

pytestmark = pytest.mark.django_db


def test_event_processing_keeps_one_call_budget_and_unapproved_working_copy(team, settings, monkeypatch):
    settings.STEP_DRAFT_BACKEND = 'openai'
    settings.STEP_DRAFT_ENDPOINT = 'http://127.0.0.1:12345/v1'
    settings.STEP_DRAFT_MODEL = 'gpt-5.4-mini'
    settings.STEP_DRAFT_API_KEY = ''
    settings.EXTERNAL_AI_ENABLED = False
    settings.FFMPEG_PATH = installed_media_binary('ffmpeg')
    settings.FFPROBE_PATH = installed_media_binary('ffprobe')
    source = settings.PRIVATE_MEDIA_ROOT / 'event-budget.mp4'
    subprocess.run([settings.FFMPEG_PATH, '-nostdin', '-v', 'error', '-f', 'lavfi',
                    '-i', 'color=c=gray:s=640x360:r=24:d=30', '-vf',
                    "drawbox=x=210:y=120:w=100:h=90:color=white:t=fill:enable='between(t,1.1,1.7)'",
                    '-an', '-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p', str(source)], check=True, timeout=30)
    asset = MediaAsset.objects.create(organization=team.org, storage_key=source.name, duration_ms=30000,
                                     width=640, height=360, status='ready', has_audio=False)
    instruction = Instruction.objects.create(organization=team.org, owner=team.users['author'], source_asset=asset)
    working = WorkingCopy.objects.create(organization=team.org, instruction=instruction, title='Тест событий и бюджета',
                                         purpose='Проверить реальный обработчик без платных запросов', language='ru')
    full_video = SimpleNamespace(asset=asset, instruction=instruction, working=working,
                                 url=f'/api/v1/orgs/{team.org.id}/instructions/{instruction.id}')
    requests = []

    class Model:
        provider, model, endpoint = 'openai', 'gpt-5.4-mini', 'https://api.openai.com/v1'

        def __init__(self, organization):
            pass

        def generate(self, *, language, mode, frames, context=None):
            requests.append(frames)
            return {'schema_version': '2', 'language': language,
                    'steps': [{'title': 'Проверить видимое изменение', 'description': 'Тестовый ответ, не проверка распознавания.',
                               'evidence_frame_ids': [frame['id'] for frame in frames]}],
                    'warnings': [{'code': 'uncertain_action', 'message': 'Нужна проверка человеком.'}]}

    monkeypatch.setattr(worker, 'VisualProvider', Model)
    upper = estimate_call('openai', 'gpt-5.4-mini', 12)['max_units']
    job, session = queue(team, full_video, max_ai_calls=1, max_cost_microusd=upper)
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done', (job.error_code, job.error_summary)
    assert len(requests) == 1 and 2 <= len(requests[0]) <= 12
    assert any(1100 <= frame['timestamp_ms'] < 1701 for frame in requests[0])
    assert all(len(frame['bytes']) <= 524288 for frame in requests[0])
    assert session.analysis_windows.count() == 1  # No paid refinement created beyond the user's cap.
    assert AIUsageReservation.objects.count() == 1
    budget = job.result['analysis']['budget']
    assert budget['calls_used'] == 1
    assert budget['spent_microusd'] + budget['reserved_microusd'] <= upper
    window = session.analysis_windows.get()
    assert window.state == 'completed'
    assert window.source_manifest['processing']['strategy'] == 'events'
    assert window.source_manifest['frame_count'] <= 12
    proposal = VisualProposal.objects.get(source_job_id=job.id)
    assert 'refinement_incomplete' in {warning['code'] for warning in proposal.content['warnings']}
    assert not full_video.working.steps.exists()
    assert job.result['metrics']['stages']['model_request']['invocations'] == 1
