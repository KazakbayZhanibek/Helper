"""Exact parent evidence survives a denser view; no model transport is used."""
from copy import deepcopy
from types import SimpleNamespace
import uuid

import pytest

from media_pipeline.exceptions import ProcessingFailure
from visual_drafts import worker
from visual_drafts.models import VisualAnalysisWindow
from .test_visual_drafts import add_frames, create_session

pytestmark = pytest.mark.django_db


@pytest.fixture
def parent_evidence(team, draft):
    session = create_session(team, draft, 'upload', analysis_budget={
        'visual_protocol': '3', 'short_action_coverage': 'v1',
    })
    frames = add_frames(session, count=3)
    manifest = {'asset_id': str(session.asset_id), 'start_ms': 0, 'end_ms': 6000,
                'frames': [{'id': str(frame.id), 'pts': frame.timestamp_ms,
                            'time_base': '1/1000', 'timestamp_ms': frame.timestamp_ms,
                            'sha256': frame.request_sha256} for frame in frames]}
    parent = VisualAnalysisWindow.objects.create(
        organization=team.org, session=session, ordinal=1, start_ms=0, end_ms=6000,
        frame_ids=[str(frame.id) for frame in frames], source_manifest=manifest,
    )
    left = VisualAnalysisWindow.objects.create(
        organization=team.org, session=session, parent=parent, ordinal=2, start_ms=0, end_ms=3000,
    )
    right = VisualAnalysisWindow.objects.create(
        organization=team.org, session=session, parent=parent, ordinal=3, start_ms=3000, end_ms=6000,
    )
    return SimpleNamespace(session=session, frames=frames, parent=parent, left=left, right=right)


def complete(parent):
    parent.state = 'completed'
    parent.save()


def test_refinement_keeps_parent_pts_in_their_own_half(parent_evidence):
    p = parent_evidence
    before = deepcopy(p.parent.source_manifest)
    complete(p.parent)
    assert worker.required_parent_frames(p.session, p.left) == [
        {'pts': 0, 'time_base': '1/1000'}, {'pts': 2000, 'time_base': '1/1000'},
    ]
    assert worker.required_parent_frames(p.session, p.right) == [{'pts': 4000, 'time_base': '1/1000'}]
    p.parent.refresh_from_db()
    assert p.parent.source_manifest == before


@pytest.mark.parametrize('problem', ['missing', 'duplicate', 'asset', 'time', 'hash', 'pts', 'base'])
def test_inconsistent_parent_manifest_stops_before_another_model_call(parent_evidence, problem):
    p = parent_evidence
    manifest = p.parent.source_manifest
    entry = manifest['frames'][0]
    if problem == 'missing':
        manifest['frames'].pop()
    elif problem == 'duplicate':
        manifest['frames'][1] = deepcopy(entry)
    elif problem == 'asset':
        manifest['asset_id'] = str(uuid.uuid4())
    elif problem == 'time':
        entry['timestamp_ms'] += 1
    elif problem == 'hash':
        entry['sha256'] = '0' * 64
    elif problem == 'pts':
        entry['pts'] = True
    elif problem == 'base':
        entry['time_base'] = '1/0'
    complete(p.parent)
    with pytest.raises(ProcessingFailure, match='Исходные кадры'):
        worker.required_parent_frames(p.session, p.left)


def test_parent_file_integrity_is_checked_before_reextracting(parent_evidence):
    p = parent_evidence
    complete(p.parent)
    path = worker.safe_frame_path(p.frames[0].storage_key)
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 1
    path.write_bytes(raw)
    with pytest.raises(ProcessingFailure) as caught:
        worker.required_parent_frames(p.session, p.left)
    assert caught.value.code == 'VISUAL_FRAME_CHANGED'


def test_only_pinned_short_action_refinements_require_parent_anchors(parent_evidence):
    p = parent_evidence
    # The overview and historical contracts retain their established sampling.
    assert worker.required_parent_frames(p.session, p.parent) == []
    assert worker.required_parent_frames(p.session, None) == []
    p.session.analysis_budget = {'visual_protocol': '3'}
    assert worker.required_parent_frames(p.session, p.left) == []


def test_unfinished_parent_and_outside_child_are_rejected(parent_evidence):
    p = parent_evidence
    with pytest.raises(ProcessingFailure):
        worker.required_parent_frames(p.session, p.left)
    complete(p.parent)
    p.left.end_ms = 6001
    with pytest.raises(ProcessingFailure):
        worker.required_parent_frames(p.session, p.left)


def test_worker_passes_exact_parent_anchors_with_unchanged_image_limit(parent_evidence, monkeypatch):
    p = parent_evidence
    complete(p.parent)
    seen = {}

    class Extracted(Exception):
        pass

    def capture(asset, context, **options):
        seen.update(options)
        raise Extracted()

    monkeypatch.setattr(worker, 'extract_video_frames', capture)
    context = SimpleNamespace(stage=lambda value: None)
    with pytest.raises(Extracted):
        worker.uploaded_frames(SimpleNamespace(), p.session, context, p.left)
    assert seen == {'start_ms': 0, 'end_ms': 3000, 'count': 12,
                    'required_frames': [{'pts': 0, 'time_base': '1/1000'},
                                        {'pts': 2000, 'time_base': '1/1000'}]}
