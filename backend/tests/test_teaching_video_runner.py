"""Offline runner guards and real quota bookkeeping with a local fake model."""
import copy
import hashlib
import importlib.util
import json
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.utils import timezone

from ai_budget.models import AIUsageReservation
from ai_drafts.providers import ProviderFailure
from ai_drafts.validation import InvalidModelOutput
from core.models import Membership
from media_pipeline.models import ProcessingJob
from visual_drafts import worker
from visual_drafts.models import VisualAnalysisWindow, VisualProposal, VisualSession
from .test_visual_drafts import add_frames

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('check_teaching_video', ROOT / 'scripts/check_teaching_video.py')
check = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = check
spec.loader.exec_module(check)


def report(experiment=check.EXPERIMENT):
    return {'version': 1, 'experiment': experiment, 'run_id': str(uuid.uuid4()), 'max_calls': 4,
            'configuration': {'fixture': True}, 'cases': [],
            'budget': {'used_or_held_calls': 0, 'settled_calls': 0, 'held_calls': 0, 'reservations': []}}


@pytest.mark.parametrize('value', ['', 'kneading,kneading', 'unknown', 'kneading,'])
def test_reject_unknown_duplicate_or_empty_clip_selection(value):
    with pytest.raises(check.runner.BenchmarkFailure, match='REGRESSION_CLIPS_INVALID'):
        check.selected_clips(value)


@pytest.mark.parametrize('change,code', [
    ({'configuration': {'changed': True}}, 'REGRESSION_CONFIGURATION_CHANGED'),
    ({'cases': [{'clip_id': 'kneading', 'status': 'done'}]}, 'REGRESSION_CASE_ALREADY_ATTEMPTED'),
    ({'cases': [{'clip_id': 'sewing', 'status': 'before_dispatch'}]}, 'REGRESSION_OUTCOME_UNRESOLVED'),
    ({'budget': {'used_or_held_calls': 1, 'held_calls': 1}}, 'REGRESSION_OUTCOME_UNRESOLVED'),
    ({'budget': {'used_or_held_calls': 4, 'held_calls': 0}}, 'REGRESSION_CALL_LIMIT'),
])
def test_resume_guards_prevent_replay_configuration_switch_or_quota_reset(change, code):
    value = report()
    value.update(change)
    with pytest.raises(check.runner.BenchmarkFailure, match=code):
        check.guard_resume(value, ['kneading'], {'fixture': True})


def test_resume_rechecks_all_requested_calls_and_prepared_jobs():
    value = report()
    value['budget']['used_or_held_calls'] = 2
    check.guard_resume(value, ['kneading', 'sewing'], {'fixture': True}, [uuid.uuid4(), uuid.uuid4()])
    with pytest.raises(check.runner.BenchmarkFailure, match='REGRESSION_CALL_LIMIT'):
        check.guard_resume(value, ['kneading', 'sewing', 'chopping'], {'fixture': True})
    with pytest.raises(check.runner.BenchmarkFailure, match='REGRESSION_CALL_LIMIT'):
        check.guard_resume(value, ['kneading'], {'fixture': True}, [uuid.uuid4() for _ in range(4)])


def test_two_output_paths_share_same_experiment_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(check, 'EXPERIMENT_LOCK', tmp_path / '.lock')
    with check.report_lock(tmp_path / 'first.json'):
        with pytest.raises(check.runner.BenchmarkFailure, match='REGRESSION_ALREADY_RUNNING'):
            with check.report_lock(tmp_path / 'second.json'):
                pytest.fail('Second runner entered')
    with check.report_lock(tmp_path / 'first.json'):
        pass


@pytest.mark.parametrize('value', ['custom-budget-reset', '../../another', '', None, ['semantic-grounding-v1']])
def test_only_explicitly_allowlisted_experiments_are_accepted(value):
    with pytest.raises(check.runner.BenchmarkFailure, match='REGRESSION_EXPERIMENT_INVALID'):
        check.checked_experiment(value)


def test_different_experiments_share_execution_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(check, 'EXPERIMENT_LOCK', tmp_path / '.lock')
    with check.report_lock(tmp_path / 'old.json', 'teaching-video-v1'):
        with pytest.raises(check.runner.BenchmarkFailure, match='REGRESSION_ALREADY_RUNNING'):
            with check.report_lock(tmp_path / 'new.json', 'semantic-grounding-v1'):
                pytest.fail('Concurrent SQLite runner entered')


@pytest.mark.parametrize('reason', ['experiment', 'configuration', 'budget'])
def test_refused_cli_resume_never_changes_historical_report(tmp_path, monkeypatch, capsys, reason):
    source_path = tmp_path / 'source.json'
    source_path.write_text('{}')
    value = report()
    value['source_report_sha256'] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if reason == 'configuration':
        value['configuration'] = {'old': True}
    if reason == 'budget':
        value['budget']['used_or_held_calls'] = 4
    output = tmp_path / 'old-report.json'
    output.write_text(json.dumps(value, indent=2) + '\n')
    original = output.read_bytes()
    experiment = 'semantic-grounding-v1' if reason == 'experiment' else 'teaching-video-v1'
    arguments = ['runner', '--experiment', experiment, '--source-report', str(source_path),
                 '--output', str(output), '--clips', 'kneading']
    if reason == 'experiment':
        arguments += ['--review-report', str(tmp_path / 'review.json')]
    monkeypatch.setattr(sys, 'argv', arguments)
    monkeypatch.setattr(check, 'EXPERIMENT_LOCK', tmp_path / '.lock')
    monkeypatch.setattr(check.runner, 'initialize_django', lambda: None)
    monkeypatch.setattr(check, 'validate_source', lambda *args: {'member': SimpleNamespace(organization_id='org', organization=object())})
    monkeypatch.setattr(check, 'configuration', lambda *args: {'fixture': True})
    monkeypatch.setattr(check, 'load_review_context', lambda _: {'cases': {'kneading': {}}})
    monkeypatch.setattr(check, 'review_preflight', lambda *args: None)
    monkeypatch.setattr(check, 'refresh_budget', lambda item: item['budget'])
    monkeypatch.setattr(check, 'experiment_jobs', lambda *args: [])
    with pytest.raises(SystemExit) as failure:
        check.main()
    assert failure.value.code == 1
    assert output.read_bytes() == original
    expected = {'experiment': 'REGRESSION_EXPERIMENT_MISMATCH', 'configuration': 'REGRESSION_CONFIGURATION_CHANGED', 'budget': 'REGRESSION_CALL_LIMIT'}[reason]
    assert expected in capsys.readouterr().out


def review_frames():
    return [{'id': str(uuid.uuid4()), 'timestamp_ms': index * 1000, 'width': 80, 'height': 60,
             'size_bytes': 100, 'sha256': 'a' * 64} for index in range(12)]


def review_candidate(frames):
    refs = [frame['id'] for frame in frames]
    return {'schema_version': '3', 'language': 'ru', 'observations': [{
        'id': 'o1', 'kind': 'action', 'subject': 'Материал', 'title': 'Сдвиньте материал',
        'before': 'Материал на столе', 'before_frame_id': refs[0], 'movement': 'Сдвиньте материал рукой.',
        'action_frame_ids': refs[1:3], 'after': 'Материал ближе к краю', 'after_frame_id': refs[3],
        'evidence_frame_ids': refs[:4]}], 'gaps': [{'reason': 'occluded', 'description': 'Движение закрыто', 'evidence_frame_ids': refs[4:6]}], 'warnings': []}


def test_review_remaps_only_frame_fields_and_preserves_input():
    old = review_frames()
    new = [{**frame, 'id': str(uuid.uuid4())} for frame in old]
    value = review_candidate(old)
    value['observations'][0]['movement'] += ' ' + old[0]['id']  # Untrusted prose is not rewritten.
    original = copy.deepcopy(value)
    mapped = check.remap_candidate(value, old, new)
    assert value == original
    assert mapped['observations'][0]['before_frame_id'] == new[0]['id']
    assert mapped['observations'][0]['after_frame_id'] == new[3]['id']
    assert mapped['observations'][0]['action_frame_ids'] == [new[1]['id'], new[2]['id']]
    assert mapped['observations'][0]['evidence_frame_ids'] == [frame['id'] for frame in new[:4]]
    assert mapped['gaps'][0]['evidence_frame_ids'] == [frame['id'] for frame in new[4:6]]
    assert mapped['observations'][0]['movement'] == original['observations'][0]['movement']


@pytest.mark.parametrize('corruption', ['foreign_ref', 'timestamp', 'hash', 'order', 'duplicate_id'])
def test_review_remap_fails_before_job_creation_for_changed_evidence(corruption):
    old = review_frames()
    new = [{**frame, 'id': str(uuid.uuid4())} for frame in old]
    value = review_candidate(old)
    if corruption == 'foreign_ref':
        value['observations'][0]['before_frame_id'] = str(uuid.uuid4())
    elif corruption == 'timestamp':
        new[0]['timestamp_ms'] += 1
    elif corruption == 'hash':
        new[0]['sha256'] = 'b' * 64
    elif corruption == 'order':
        new.reverse()
    else:
        new[1]['id'] = new[0]['id']
    with pytest.raises(check.runner.BenchmarkFailure):
        check.remap_candidate(value, old, new)


def test_review_frozen_configuration_binds_candidate_report_and_policy(monkeypatch, tmp_path):
    from visual_drafts import review
    monkeypatch.setattr(check.runner, 'safe_config', lambda _: {'provider': 'codex', 'model': 'gpt-6-luna', 'mode': 'subscription_calls'})
    frames = review_frames()
    context = {'source_report_sha256': 'a' * 64, 'cases': {'kneading': {
        'candidate': review_candidate(frames), 'sent_frames': frames, 'source_job_id': str(uuid.uuid4())}}}
    original = check.configuration(object(), context)
    changed = copy.deepcopy(context)
    changed['cases']['kneading']['candidate']['observations'][0]['movement'] += ' Уточнение.'
    assert check.configuration(object(), changed) != original
    changed = copy.deepcopy(context)
    changed['source_report_sha256'] = 'b' * 64
    assert check.configuration(object(), changed) != original
    real_contract = review.request_review_contract
    def changed_policy(*args, **kwargs):
        system, schema, request = real_contract(*args, **kwargs)
        return system + '\nChanged policy.', schema, request
    monkeypatch.setattr(review, 'request_review_contract', changed_policy)
    assert check.configuration(object(), context) != original


def test_reviewer_override_restores_provider_after_exception(monkeypatch):
    from visual_drafts import review
    original = worker.VisualProvider
    candidate = {'unverified': True}
    calls = []
    monkeypatch.setattr(review, 'VisualReviewProvider', lambda organization, *, candidate: calls.append((organization, candidate)))
    with pytest.raises(RuntimeError):
        with check.use_reviewer(candidate):
            worker.VisualProvider('organization')
            raise RuntimeError('test')
    assert worker.VisualProvider is original
    assert calls == [('organization', candidate)]


@pytest.mark.parametrize('reject', [False, True])
def test_validation_capture_forwards_unchanged_and_always_restores(monkeypatch, reject):
    raw = {'observations': []}
    expected = {'steps': []}
    case = {}
    calls = []
    def validate(value, *args, **kwargs):
        assert value is raw
        calls.append(1)
        if reject:
            raise InvalidModelOutput('VISUAL_EVIDENCE_PHASE_INVALID')
        return expected
    monkeypatch.setattr(worker, 'validate', validate)
    if reject:
        with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_PHASE_INVALID'):
            with check.capture_validation(case):
                worker.validate(raw, [], object(), expected_version='3')
        assert case['validation'] == {'status': 'rejected', 'code': 'VISUAL_EVIDENCE_PHASE_INVALID'}
    else:
        with check.capture_validation(case):
            assert worker.validate(raw, [], object(), expected_version='3') is expected
        assert case['validation'] == {'status': 'passed'}
    assert worker.validate is validate and calls == [1]
    assert case['raw_model_response'] == raw and case['raw_model_response'] is not raw


def source(team, draft, clip='kneading'):
    instruction = draft.instruction if hasattr(draft, 'instruction') else None
    if instruction is None:
        from core.models import Instruction
        instruction = Instruction.objects.get(pk=draft.working['instruction_id'])
    session = VisualSession.objects.create(organization=team.org, instruction=instruction, asset=instruction.source_asset,
        created_by=team.users['author'], mode='upload', state='active', consent_images=True,
        base_working_revision=instruction.working_copy.revision, language='ru',
        window_start_ms=0, window_end_ms=24000, expires_at=timezone.now() + timedelta(hours=1))
    frames = add_frames(session, 12)
    window = VisualAnalysisWindow.objects.create(organization=team.org, session=session, ordinal=1,
        start_ms=0, end_ms=24000, frame_ids=[str(frame.id) for frame in frames], state='completed')
    session.state = 'completed'
    session.save(update_fields=['state'])
    from visual_drafts.services import safe_frame_path
    member = Membership.objects.select_related('organization', 'user').get(organization=team.org, user=team.users['author'])
    old_case = {'job_id': str(uuid.uuid4()), 'proposal': {}, 'sent_frames': [
        {key: str(getattr(frame, key)) if key == 'id' else getattr(frame, key) for key in check.FRAME_FIELDS} for frame in frames]}
    return {'clip_id': clip, 'old_session': session, 'old_case': old_case, 'member': member,
            'working_before': check.fingerprint_working(instruction), 'old_window': window,
            'evidence': [(frame, safe_frame_path(frame.storage_key).read_bytes()) for frame in frames]}


def install_local_model(monkeypatch, *, failure=False):
    calls = []
    class LocalModel:
        provider, model, endpoint = 'codex', 'gpt-6-luna', ''
        def __init__(self, organization):
            pass
        def prepare_request(self, **kwargs):
            assert self.protocol == '3'
        def generate(self, *, language, mode, frames, context):
            calls.append(1)
            if failure:
                raise ProviderFailure('MODEL_TIMEOUT', 'Offline fixture')
            refs = [frame['id'] for frame in frames]
            return {'schema_version': '3', 'language': language, 'observations': [{
                'id': 'o1', 'kind': 'action', 'subject': 'Материал', 'title': 'Сдвиньте материал',
                'before': None, 'before_frame_id': None, 'movement': 'Сдвиньте материал рукой.',
                'action_frame_ids': refs, 'after': None, 'after_frame_id': None, 'evidence_frame_ids': refs}],
                'gaps': [], 'warnings': []}
    monkeypatch.setattr(worker, 'capabilities', lambda _: {'available': True})
    monkeypatch.setattr(worker, 'VisualProvider', LocalModel)
    monkeypatch.setattr(check, 'configuration', lambda _: {'fixture': True})
    return calls


@pytest.mark.django_db
def test_exact_replay_one_receipt_preserved_working_copy_and_no_replay(team, draft, monkeypatch):
    item = source(team, draft)
    calls = install_local_model(monkeypatch)
    value, checkpoints = report(), []
    check.run([item], value, lambda: checkpoints.append(copy.deepcopy(value)))
    assert len(calls) == AIUsageReservation.objects.count() == 1
    assert value['budget']['settled_calls'] == 1 and value['budget']['held_calls'] == 0
    assert value['cases'][0]['identical_images_and_times'] is True
    assert value['cases'][0]['validation']['status'] == 'passed'
    assert value['cases'][0]['raw_model_response']['schema_version'] == '3'
    assert value['cases'][0]['working_before'] == value['cases'][0]['working_after']
    assert any(point['cases'][0]['status'] == 'before_dispatch' for point in checkpoints)
    proposal = VisualProposal.objects.get()
    assert proposal.content['schema_version'] == '3' and proposal.applied_at is None
    job = ProcessingJob.objects.get()
    assert job.payload['visual_protocol'] == '3' and job.max_attempts == 1
    with pytest.raises(check.runner.BenchmarkFailure, match='REGRESSION_CASE_ALREADY_ATTEMPTED'):
        check.run([item], value, lambda: None)
    assert len(calls) == 1


@pytest.mark.django_db
def test_unknown_receipt_stops_before_second_clip_and_blocks_resume(team, draft, monkeypatch):
    first = source(team, draft)
    second = {**first, 'clip_id': 'sewing'}
    calls = install_local_model(monkeypatch, failure=True)
    value = report()
    with pytest.raises(check.runner.BenchmarkFailure, match='REGRESSION_STOPPED_NO_RETRY'):
        check.run([first, second], value, lambda: None)
    assert calls == [1] and len(value['cases']) == 1
    assert value['budget']['held_calls'] == 1
    with pytest.raises(check.runner.BenchmarkFailure, match='REGRESSION_OUTCOME_UNRESOLVED'):
        check.run([second], value, lambda: None)
    assert calls == [1]


@pytest.mark.django_db
def test_named_experiments_have_independent_four_call_caps_that_new_reports_cannot_reset(team, draft, monkeypatch):
    first = source(team, draft)
    sources = [{**first, 'clip_id': clip} for clip in check.CLIPS]
    calls = install_local_model(monkeypatch)
    old, new = report(), report('semantic-grounding-v1')
    check.run(sources, old, lambda: None)
    assert old['budget']['used_or_held_calls'] == 4
    assert len(check.experiment_jobs()) == 4
    assert check.experiment_jobs('semantic-grounding-v1') == []
    old_snapshot = copy.deepcopy(old)
    check.run(sources, new, lambda: None)
    assert len(calls) == AIUsageReservation.objects.count() == 8
    assert old == old_snapshot
    assert new['budget']['used_or_held_calls'] == new['budget']['settled_calls'] == 4
    assert new['budget']['held_calls'] == 0
    assert len(check.experiment_jobs('semantic-grounding-v1')) == 4
    for experiment in check.EXPERIMENTS:
        fresh = report(experiment)
        check.refresh_budget(fresh)
        with pytest.raises(check.runner.BenchmarkFailure, match='REGRESSION_CALL_LIMIT'):
            check.run([first], fresh, lambda: None)
    assert len(calls) == 8


@pytest.mark.django_db
def test_review_only_uses_one_real_budget_reservation_and_remapped_candidate(team, draft, monkeypatch):
    from visual_drafts import review
    first = source(team, draft)
    old_frames = first['old_case']['sent_frames']
    candidate = review_candidate(old_frames)
    original_candidate = copy.deepcopy(candidate)
    context = {'source_report_sha256': 'a' * 64, 'cases': {'kneading': {
        'candidate': candidate, 'sent_frames': old_frames, 'source_job_id': first['old_case']['job_id']}}}
    invocations = []
    class Reviewer:
        provider, model, endpoint = 'codex', 'gpt-6-luna', ''
        protocol = '3'
        def __init__(self, organization, *, candidate):
            self.candidate = candidate
        def prepare_request(self, *, frames, **kwargs):
            valid = {frame['id'] for frame in frames}
            assert set(self.candidate['observations'][0]['evidence_frame_ids']).issubset(valid)
        def generate(self, *, frames, **kwargs):
            invocations.append(copy.deepcopy(self.candidate))
            assert self.candidate['observations'][0]['before_frame_id'] != old_frames[0]['id']
            return copy.deepcopy(self.candidate)
    monkeypatch.setattr(review, 'VisualReviewProvider', Reviewer)
    monkeypatch.setattr(worker, 'capabilities', lambda _: {'available': True})
    monkeypatch.setattr(check, 'configuration', lambda *args: {'fixture': True})
    value = report('semantic-grounding-v1')
    original_provider = worker.VisualProvider
    check.run([first], value, lambda: None, context)
    assert worker.VisualProvider is original_provider
    assert len(invocations) == AIUsageReservation.objects.count() == 1
    assert value['budget']['settled_calls'] == 1 and value['budget']['held_calls'] == 0
    assert value['cases'][0]['review']['mode'] == 'review_only'
    assert value['cases'][0]['review']['source_report_sha256'] == 'a' * 64
    assert value['cases'][0]['review']['candidate'] == invocations[0]
    assert candidate == original_candidate
    assert value['cases'][0]['working_before'] == value['cases'][0]['working_after']
