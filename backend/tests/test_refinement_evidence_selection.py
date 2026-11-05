"""Retain complete denser evidence, not the most confident-looking response.

All model outputs are local fixtures. These tests establish selection and
accounting behavior; they do not establish image-recognition accuracy.
"""
from copy import deepcopy
from types import SimpleNamespace
import hashlib
import uuid

import pytest

from ai_budget.models import AIUsageReservation
from media_pipeline.worker import process_one
from visual_drafts import planning, provider, services, short_actions, worker
from visual_drafts.models import VisualProposal
from .test_action_protocol import action_value
from .test_visual_full_video import full_video, queue
from .test_short_action_coverage import coverage_video
from .test_visual_drafts import jpeg


def selected_session(**pins):
    return SimpleNamespace(mode='upload', language='ru', asset=None,
                           window_start_ms=0, window_end_ms=60000,
                           analysis_budget={'visual_protocol': '3', 'short_action_coverage': 'v1', **pins})


def raw_value(frames, *, action=True, uncertain=False, state=False):
    value = action_value(frames)
    value['schema_version'] = short_actions.WIRE_VERSION
    if state:
        value['observations'][0].update(kind='state', movement=None, action_frame_ids=[],
                                        before='Предмет находится на поверхности.',
                                        before_frame_id=str(frames[0]['id']))
    elif not action:
        value['observations'] = []
    value['transitions'] = [
        {'pair_index': index,
         'change': 'visible_action' if action and not state else 'unexplained_change' if uncertain else 'no_clear_change',
         'description': 'На этих кадрах часть взаимодействия закрыта рукой.' if uncertain else 'Рука меняет положение предмета.',
         'observation_ids': ['o1'] if action or state else []}
        for index in range(len(frames) - 1)
    ]
    if uncertain:
        value['gaps'] = [{'reason': 'occluded', 'description': 'Рука закрывает часть действия.',
                          'evidence_frame_ids': [str(frame['id']) for frame in frames]}]
    return value


def normalized(start, end, **options):
    midpoint = (start + end) // 2
    frames = [SimpleNamespace(id=uuid.uuid4(), sequence=i + 1, timestamp_ms=timestamp,
                              coverage_start_ms=timestamp, coverage_end_ms=limit)
              for i, (timestamp, limit) in enumerate(((start, midpoint), (midpoint, end)))]
    data = [{'id': str(frame.id), 'sequence': frame.sequence, 'timestamp_ms': frame.timestamp_ms} for frame in frames]
    return short_actions.normalize_short_actions(raw_value(data, **options), frames, selected_session())


def window(start, end, *, parent=None, status='completed', **options):
    content = normalized(start, end, **options)
    refs = [content['temporal_coverage']['pairs'][0]['from_frame_id'],
            content['temporal_coverage']['pairs'][0]['to_frame_id']]
    return SimpleNamespace(id=uuid.uuid4(), parent_id=parent, start_ms=start, end_ms=end,
                           state=status, content=content, frame_ids=refs)


def fingerprints(session, windows):
    # Pure selection fixtures stand in for the scoped persisted-frame query.
    # Every midpoint parent's timestamp appears in the partition's right half.
    result = {}
    for item in windows:
        times = [item.start_ms, (item.start_ms + item.end_ms) // 2]
        result.update({ref: (timestamp, hashlib.sha256(str(timestamp).encode()).hexdigest())
                       for ref, timestamp in zip(item.frame_ids, times)})
    return result


@pytest.fixture
def aggregate(monkeypatch):
    monkeypatch.setattr(planning, 'analysis_data', lambda session: {'budget': {'calls_used': 0}})
    monkeypatch.setattr(planning, '_frame_fingerprints', fingerprints)
    return lambda windows, session=None: planning.aggregate_content(session or selected_session(), windows)


@pytest.mark.parametrize('count', [2, 4])
def test_full_partition_keeps_denser_actions_gaps_and_all_frame_links(aggregate, count):
    parent = window(0, count * 15000)
    children = [window(i * 15000, (i + 1) * 15000, parent=parent.id, uncertain=True)
                for i in range(count)]
    original = deepcopy([item.content for item in [parent, *children]])
    result = aggregate([parent, *reversed(children)])
    assert len(result['steps']) == count
    assert {item['id'] for item in result['gaps']} == {gap['id'] for child in children for gap in child.content['gaps']}
    assert services.evidence_ids(result) == set().union(*(services.evidence_ids(child.content) for child in children))
    assert not services.evidence_ids(parent.content).intersection(services.evidence_ids(result))
    assert {pair['window_id'] for pair in result['temporal_coverage']['pairs']} == {str(child.id) for child in children}
    codes = [item['code'] for item in result['warnings']]
    assert 'occluded_view' in codes and 'refinement_unresolved' not in codes
    assert len(codes) == len(set(codes))
    result['temporal_coverage']['pairs'][0]['observation_ids'].append('mutation')
    assert [item.content for item in [parent, *children]] == original


@pytest.mark.parametrize('uncertain', [True, False])
def test_completed_empty_children_do_not_resurrect_parent_commands(aggregate, uncertain):
    parent = window(0, 12000)
    children = [window(0, 6000, parent=parent.id, action=False, uncertain=uncertain),
                window(6000, 12000, parent=parent.id, action=False, uncertain=uncertain)]
    result = aggregate([parent, *children])
    assert result['steps'] == result['observations'] == []
    assert len(result['gaps']) == (2 if uncertain else 0)
    assert result['temporal_coverage']['unresolved_pairs'] == (2 if uncertain else 0)
    assert services.evidence_ids(result) == set().union(*(services.evidence_ids(child.content) for child in children))
    assert 'insufficient_evidence' in {item['code'] for item in result['warnings']}


def test_empty_or_state_only_part_is_preserved_alongside_a_child_action(aggregate):
    parent = window(0, 12000)
    left = window(0, 6000, parent=parent.id, action=False, uncertain=True, state=True)
    right = window(6000, 12000, parent=parent.id)
    result = aggregate([parent, left, right])
    assert result['steps'] == right.content['steps']
    assert result['gaps'] == left.content['gaps']
    assert {item['kind'] for item in result['observations']} == {'action', 'state'}
    assert result['temporal_coverage']['unresolved_pairs'] == 1


@pytest.mark.parametrize('state', ['pending', 'running', 'failed'])
def test_incomplete_child_set_retains_parent_with_warning(aggregate, state):
    parent = window(0, 12000)
    left = window(0, 6000, parent=parent.id, action=False, uncertain=True)
    right = window(6000, 12000, parent=parent.id, status=state)
    result = aggregate([parent, left, right])
    assert result['steps'] == parent.content['steps']
    assert services.evidence_ids(result) == services.evidence_ids(parent.content)
    assert 'refinement_unresolved' in {item['code'] for item in result['warnings']}


@pytest.mark.parametrize('parts', [
    [(0, 6000)], [(0, 12000)], [(0, 5000), (6000, 12000)],
    [(0, 7000), (6000, 12000)], [(1, 6000), (6000, 12000)],
    [(0, 6000), (6000, 11999)], [(0, 6000), (6000, 12001)],
    [(0, 6000), (0, 6000), (6000, 12000)],
])
def test_missing_nonpartition_or_duplicate_intervals_never_replace_parent(aggregate, parts):
    parent = window(0, 12000)
    children = [window(start, end, parent=parent.id) for start, end in parts]
    result = aggregate([parent, *children])
    assert result['steps'] == parent.content['steps']
    assert 'refinement_unresolved' in {item['code'] for item in result['warnings']}


@pytest.mark.parametrize('key,value', [
    ('schema_version', '2'), ('step_format', None), ('language', 'kk'),
    ('steps', None), ('observations', None), ('gaps', None), ('warnings', None),
    ('temporal_coverage', None), ('temporal_coverage', {'version': 'v0', 'pairs': [], 'unresolved_pairs': 0}),
    ('temporal_coverage', {'version': 'v1', 'pairs': [{}], 'unresolved_pairs': 1}),
    ('temporal_coverage', {'version': 'v1', 'pairs': [], 'unresolved_pairs': 0}),
])
def test_missing_or_mismatched_normalized_contract_retains_parent(aggregate, key, value):
    parent = window(0, 12000)
    left, right = window(0, 6000, parent=parent.id), window(6000, 12000, parent=parent.id)
    right.content[key] = value
    result = aggregate([parent, left, right])
    assert result['steps'] == parent.content['steps']
    assert 'refinement_unresolved' in {item['code'] for item in result['warnings']}


@pytest.mark.parametrize('mode,protocol,pin', [('upload', '2', None), ('upload', '3', None), ('live', '3', 'v1')])
def test_legacy_and_unpinned_modes_keep_historical_uncertain_child_fallback(aggregate, mode, protocol, pin):
    session = selected_session(visual_protocol=protocol, short_action_coverage=pin)
    session.mode = mode
    parent = window(0, 12000)
    children = [window(0, 6000, parent=parent.id, uncertain=True),
                window(6000, 12000, parent=parent.id, action=False, uncertain=True)]
    result = aggregate([parent, *children], session)
    assert result['steps'] == parent.content['steps']
    assert 'refinement_unresolved' in {item['code'] for item in result['warnings']}


def test_refined_selection_does_not_bypass_an_enabled_review(aggregate):
    session = selected_session(semantic_review='v1')
    parent = window(0, 12000)
    children = [window(0, 6000, parent=parent.id), window(6000, 12000, parent=parent.id)]
    result = aggregate([parent, *children], session)
    assert result['steps'] == parent.content['steps']
    assert 'refinement_unresolved' in {item['code'] for item in result['warnings']}


@pytest.mark.parametrize('loss', ['pixels', 'timestamp', 'missing_parent', 'missing_child'])
def test_completed_partition_must_preserve_every_original_image_and_time(aggregate, monkeypatch, loss):
    parent = window(0, 12000)
    left = window(0, 6000, parent=parent.id, action=False, uncertain=True)
    right = window(6000, 12000, parent=parent.id, action=False, uncertain=True)
    resolved = fingerprints(None, [parent, left, right])
    target = right.frame_ids[0]
    if loss == 'pixels':
        timestamp, _ = resolved[target]
        resolved[target] = (timestamp, 'f' * 64)
    elif loss == 'timestamp':
        timestamp, digest = resolved[target]
        resolved[target] = (timestamp + 1, digest)
    elif loss == 'missing_parent':
        del resolved[parent.frame_ids[0]]
    else:
        del resolved[right.frame_ids[1]]
    monkeypatch.setattr(planning, '_frame_fingerprints', lambda *_: resolved)
    result = aggregate([parent, left, right])
    assert result['steps'] == parent.content['steps']
    assert 'refinement_unresolved' in {item['code'] for item in result['warnings']}


def extraction_with_retained_images(job, session, context, window):
    """Real persisted JPEGs; the same timestamp always has the same bytes."""
    if window.frame_ids:
        return list(session.frames.filter(id__in=window.frame_ids).order_by('timestamp_ms'))
    midpoint = (window.start_ms + window.end_ms) // 2
    bounds = [window.start_ms, midpoint, window.end_ms]
    sequence = session.frames.count()
    frames = []
    for index in range(2):
        raw = jpeg('red' if bounds[index] == 6000 else 'blue')
        frames.append(services.persist_frame(
            session, sequence=sequence + index + 1, timestamp_ms=bounds[index],
            coverage_start_ms=bounds[index], coverage_end_ms=bounds[index + 1],
            content=raw, width=80, height=60, idempotency_key=f'{window.id}:{index}',
            request_sha256=hashlib.sha256(raw).hexdigest()))
    window.frame_ids = [str(frame.id) for frame in frames]
    window.save(update_fields=['frame_ids'])
    return frames


@pytest.mark.django_db
def test_worker_keeps_real_normalized_empty_refinements_with_same_three_call_budget(team, coverage_video, monkeypatch):
    calls = []

    class LocalModel(provider.VisualProvider):
        def __init__(self, organization):
            self.provider, self.model, self.endpoint = 'codex', 'gpt-6-luna', ''

        def generate(self, *, language, mode, frames, context=None):
            times = [frame['timestamp_ms'] for frame in frames]
            calls.append(times)
            # The overview is confident in a command but also requests denser
            # evidence. Both denser calls explain that the method is obscured.
            return raw_value(frames, action=len(calls) == 1, uncertain=True)

    monkeypatch.setattr(worker, 'VisualProvider', LocalModel)
    monkeypatch.setattr(worker, 'uploaded_frames', extraction_with_retained_images)
    job, session = queue(team, coverage_video, scope='window', start_ms=0, end_ms=12000, max_ai_calls=3)
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done', (job.error_code, job.error_summary)
    assert calls == [[0, 6000], [0, 3000], [6000, 9000]]
    proposal = VisualProposal.objects.get(source_job_id=job.id)
    assert proposal.content['steps'] == []
    assert len(proposal.content['gaps']) == 2
    assert not services.proposal_data(proposal)['can_apply']
    assert proposal.content['analysis']['budget']['calls_used'] == 3
    assert AIUsageReservation.objects.filter(session_id=session.id).count() == 3
    assert all(receipt.state == 'settled' and receipt.actual_units == 1
               for receipt in AIUsageReservation.objects.filter(session_id=session.id))
    assert session.analysis_windows.filter(parent__isnull=False, state='completed').count() == 2
    assert not coverage_video.working.steps.exists()
    windows = list(session.analysis_windows.all())
    resolved = planning._frame_fingerprints(session, windows)
    assert len(resolved) == 6
    assert planning._frame_fingerprints(SimpleNamespace(id=session.id, organization_id=team.other.id), windows) == {}
    assert planning._frame_fingerprints(SimpleNamespace(id=uuid.uuid4(), organization_id=team.org.id), windows) == {}
