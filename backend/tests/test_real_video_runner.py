"""The comparison runner uses real quotas with a fake model, never paid requests."""
import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

from ai_budget.models import AIUsageReservation
from core.models import Instruction, Membership, WorkingCopy
from media_pipeline.models import ProcessingJob
from visual_drafts import worker
from .test_visual_full_video import fake_extraction

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
# Other runner suites import this shared module during collection too. Reuse
# that instance so its failure type/locks are identical across those runners.
runner = importlib.import_module('compare_real_video')


def manifest(tmp_path, **changes):
    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'transport fixture')
    value = {'clips': [{'id': 'clip', 'path': source.name, 'rights_confirmed': True,
                        'source_url': 'https://example.com/source', 'license_url': 'https://example.com/license', **changes}]}
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(value))
    return path


@pytest.mark.parametrize('change', [{'rights_confirmed': False}, {'rights_confirmed': 1},
                                    {'source_url': 'file:///private/video'}, {'id': '../../escape'}])
def test_manifest_fails_closed_before_django_or_model(tmp_path, change):
    with pytest.raises(runner.BenchmarkFailure):
        runner.manifest_entries(manifest(tmp_path, **change), 8)


def test_manifest_rejects_over_budget_and_duplicate_sources(tmp_path):
    path = manifest(tmp_path)
    data = json.loads(path.read_text())
    data['clips'] *= 2
    path.write_text(json.dumps(data))
    with pytest.raises(runner.BenchmarkFailure):
        runner.manifest_entries(path, 2)
    with pytest.raises(runner.BenchmarkFailure):
        runner.manifest_entries(path, 8)
    with pytest.raises(runner.BenchmarkFailure):
        runner.manifest_entries(path, 9)


@pytest.mark.django_db
def test_comparison_records_two_settled_calls_real_quotas_and_no_authoring(team, monkeypatch, tmp_path):
    configuration = {'provider': 'codex', 'model': 'fake-test-only'}
    monkeypatch.setattr(runner, 'safe_config', lambda org: configuration)
    monkeypatch.setattr(worker, 'capabilities', lambda org: {'available': True})
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
    invocations = []

    class FakeModel:
        provider, model, endpoint = 'codex', 'fake-test-only', ''
        def __init__(self, org):
            pass
        def generate(self, *, language, mode, frames, context):
            invocations.append(context.job_id)
            return {'schema_version': '2', 'language': language,
                    'steps': [{'title': 'Тестовое наблюдение', 'description': 'Тест не оценивает качество модели.',
                               'evidence_frame_ids': [frame['id'] for frame in frames]}],
                    'warnings': [{'code': 'uncertain_action', 'message': 'Для проверки бюджета требуется уточнение.'}]}
    monkeypatch.setattr(worker, 'VisualProvider', FakeModel)
    source = tmp_path / 'fixture.mp4'
    source.write_bytes(b'not decoded: uploaded_frames is replaced with test JPEGs')
    clip = runner.PreparedClip('clip', source, hashlib.sha256(source.read_bytes()).hexdigest(),
                              {'width': 80, 'height': 60, 'duration_ms': 12000, 'has_audio': False},
                              {'rights_confirmed': True}, source.stat().st_size)
    member = Membership.objects.select_related('organization', 'user').get(organization=team.org, user=team.users['author'])
    report = {'run_id': '897d2dc0-d998-45a9-9c43-43485762096a', 'configuration': configuration,
              'cases': [], 'sources': []}
    checkpoints = []
    runner.execute_comparison(member, [clip], report, lambda: checkpoints.append(len(report['cases'])), max_calls=2)
    assert report['status'] == 'completed'
    assert len(invocations) == AIUsageReservation.objects.count() == 2
    assert report['budget']['used_or_held_calls'] == report['budget']['settled_calls'] == 2
    assert report['budget']['held_calls'] == 0 and report['budget']['usd_cost'] is None
    assert [case['strategy'] for case in report['cases']] == ['uniform_legacy', 'events']
    assert all(len(case['windows']) == 1 for case in report['cases'])
    assert all(case['sent_frames'][0]['sha256'] for case in report['cases'])
    assert all(job.state == 'done' and job.max_attempts == 1 for job in ProcessingJob.objects.all())
    assert Instruction.objects.count() == WorkingCopy.objects.count() == 1
    working = WorkingCopy.objects.get()
    assert working.revision == 1 and not working.steps.exists()
    assert checkpoints.count(1) >= 2 and checkpoints.count(2) >= 2


@pytest.mark.django_db
def test_unknown_call_halts_before_second_strategy_and_keeps_reservation(team, monkeypatch, tmp_path):
    from ai_drafts.providers import ProviderFailure
    configuration = {'provider': 'codex', 'model': 'fake-test-only'}
    monkeypatch.setattr(runner, 'safe_config', lambda org: configuration)
    monkeypatch.setattr(worker, 'capabilities', lambda org: {'available': True})
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
    class FailingModel:
        provider, model, endpoint = 'codex', 'fake-test-only', ''
        def __init__(self, org):
            pass
        def generate(self, **kwargs):
            raise ProviderFailure('MODEL_TIMEOUT', 'Тест неизвестного исхода')
    monkeypatch.setattr(worker, 'VisualProvider', FailingModel)
    source = tmp_path / 'fixture.mp4'
    source.write_bytes(b'fixture')
    clip = runner.PreparedClip('clip', source, hashlib.sha256(source.read_bytes()).hexdigest(),
                              {'width': 80, 'height': 60, 'duration_ms': 12000, 'has_audio': False}, {}, source.stat().st_size)
    member = Membership.objects.select_related('organization', 'user').get(organization=team.org, user=team.users['author'])
    report = {'run_id': 'd3c19444-3116-4500-a95f-e48ad39d5d8f', 'configuration': configuration, 'cases': [], 'sources': []}
    with pytest.raises(runner.BenchmarkFailure, match='BENCHMARK_CASE_FAILED_NO_RETRY'):
        runner.execute_comparison(member, [clip], report, lambda: None, max_calls=2)
    assert len(report['cases']) == AIUsageReservation.objects.count() == 1
    assert report['budget']['held_calls'] == 1 and report['budget']['settled_calls'] == 0
    assert ProcessingJob.objects.get().max_attempts == 1
