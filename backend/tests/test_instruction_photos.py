"""Photographs follow frozen publication references, without any model calls."""
import copy
import hashlib
import io
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from PIL import Image
from django.utils import timezone

from core import services as core
from core.models import Instruction, Membership, PublishedVersion, WorkingStep
from visual_drafts import provider, services
from visual_drafts.models import VisualProposal, VisualSession
from .conftest import CONFIRMATIONS, base, checked_review, document, post, save


pytestmark = pytest.mark.django_db


def evidence(team, draft, *, count=3, mode='upload'):
    instruction = Instruction.objects.get(pk=draft.working['instruction_id'])
    session = VisualSession.objects.create(
        organization=team.org, instruction=instruction,
        asset=instruction.source_asset if mode == 'upload' else None,
        created_by=team.users['author'], mode=mode, state='completed',
        consent_images=True, base_working_revision=draft.working['revision'],
        language='ru', expires_at=timezone.now() + timedelta(hours=1),
        window_start_ms=0, window_end_ms=60000,
    )
    output = io.BytesIO()
    Image.new('RGB', (80, 60), 'blue').save(output, format='JPEG')
    raw = output.getvalue()
    frames = [services.persist_frame(
        session, sequence=index, timestamp_ms=index * 1000,
        coverage_start_ms=index * 1000, coverage_end_ms=(index + 1) * 1000,
        content=raw, width=80, height=60, idempotency_key=f'photo-{index}',
        request_sha256=hashlib.sha256(raw).hexdigest(),
    ) for index in range(count)]
    return session, frames


def proposal(team, session, frames):
    content = provider.validate({
        'schema_version': '2', 'language': 'ru', 'warnings': [],
        'steps': [{'title': 'Передвиньте деталь', 'description': 'Передвиньте деталь вправо по поверхности.',
                   'evidence_frame_ids': [str(frame.id) for frame in frames[:2]]}],
    }, frames, session)
    return VisualProposal.objects.create(
        organization=team.org, session=session, source_job_id=uuid.uuid4(),
        provider='local-test', model='no-model-call', content=content,
    )


def image_url(team, frame):
    return f'/api/v1/orgs/{team.org.id}/visual-frames/{frame.id}'


@pytest.fixture
def illustrated(team, draft):
    session, frames = evidence(team, draft)
    item = proposal(team, session, frames)
    applied = post(team.clients['author'], draft.url + f'/visual-proposals/{item.id}/apply',
                   {'confirm_replace': True}, etag=draft.working['etag'], key='apply-photos')
    assert applied.status_code == 200, applied.json()
    draft.working = applied.json()
    reviewed = checked_review(team, draft)
    published = post(team.clients['reviewer'], draft.url + '/publish',
                     {'confirmations': CONFIRMATIONS}, etag=reviewed['etag'], key='publish-photos')
    assert published.status_code == 201, published.json()
    session.instruction.refresh_from_db()
    return SimpleNamespace(session=session, frames=frames, item=item, draft=draft,
                           version=published.json(), instruction=session.instruction)


def replace_snapshot(team, illustrated, mutate):
    """Create a distinct immutable version; never update an existing snapshot."""
    instruction = illustrated.instruction
    snapshot = copy.deepcopy(illustrated.version['snapshot'])
    mutate(snapshot)
    version = PublishedVersion.objects.create(
        organization=team.org, instruction=instruction,
        version_number=instruction.versions.count() + 1,
        source_asset=instruction.source_asset, snapshot=snapshot,
        content_sha256='0' * 64, source_working_revision=instruction.working_copy.revision,
        published_by=team.users['reviewer'],
    )
    instruction.current_version = version
    instruction.save(update_fields=['current_version'])
    return version


def test_apply_edit_publish_preserves_photos_and_freezes_metadata(team, illustrated):
    step = illustrated.version['snapshot']['steps'][0]
    expected = [str(frame.id) for frame in illustrated.frames[:2]]
    assert step['evidence_frame_ids'] == expected
    assert [frame['id'] for frame in step['evidence_frames']] == expected
    assert [frame['timestamp_ms'] for frame in step['evidence_frames']] == [0, 1000]
    assert step['evidence_frames'][0]['url'] == image_url(team, illustrated.frames[0])
    frozen = copy.deepcopy(illustrated.version['snapshot'])
    opened = post(team.clients['author'], illustrated.draft.url + '/create-draft',
                  {'source_version_id': illustrated.version['id']}, key='new-photo-draft')
    assert opened.status_code == 201, opened.json()
    current = opened.json()
    payload = document(current)
    payload['steps'][0]['description'] = 'Автор уточнил движение по кадрам.'
    edited = save(team.clients['author'], illustrated.draft.url, current, payload)
    assert edited.status_code == 200, edited.json()
    assert edited.json()['steps'][0]['evidence_frames'] == step['evidence_frames']
    assert team.clients['reader'].get(illustrated.draft.url).json()['publication']['snapshot'] == frozen


@pytest.mark.parametrize('role', ['reader', 'author', 'reviewer', 'admin'])
def test_published_photo_available_with_private_no_store(team, illustrated, role):
    result = team.clients[role].get(image_url(team, illustrated.frames[0]))
    assert result.status_code == 200
    assert result['Content-Type'] == 'image/jpeg'
    assert result['Cache-Control'] == 'private, no-store'
    assert b''.join(result.streaming_content).startswith(b'\xff\xd8')


def test_reader_cannot_see_unpublished_or_unknown_frames(team, draft):
    session, frames = evidence(team, draft)
    assert team.clients['reader'].get(image_url(team, frames[0])).status_code == 403
    assert team.clients['reader'].get(image_url(team, SimpleNamespace(id=uuid.uuid4()))).status_code == 403
    assert team.clients['author'].get(image_url(team, frames[0])).status_code == 200


def test_publication_does_not_grant_whole_session(team, illustrated):
    assert team.clients['reader'].get(image_url(team, illustrated.frames[2])).status_code == 403
    assert team.clients['author'].get(image_url(team, illustrated.frames[2])).status_code == 200


def test_reader_cannot_see_frame_from_old_version(team, illustrated):
    frozen = copy.deepcopy(illustrated.version['snapshot'])
    replace_snapshot(team, illustrated, lambda snapshot: snapshot['steps'][0].update(
        evidence_frame_ids=[], evidence_frames=[], evidence_visual_session_id=None))
    assert team.clients['reader'].get(image_url(team, illustrated.frames[0])).status_code == 403
    assert PublishedVersion.objects.get(pk=illustrated.version['id']).snapshot == frozen


@pytest.mark.parametrize('mismatch', ['session', 'source', 'frame'])
def test_reader_requires_matching_session_source_and_explicit_id(team, illustrated, mismatch):
    def alter(snapshot):
        if mismatch == 'session':
            snapshot['steps'][0]['evidence_visual_session_id'] = str(uuid.uuid4())
        elif mismatch == 'source':
            snapshot['source']['id'] = str(uuid.uuid4())
        else:
            snapshot['steps'][0]['evidence_frame_ids'] = [str(uuid.uuid4())]
    replace_snapshot(team, illustrated, alter)
    assert team.clients['reader'].get(image_url(team, illustrated.frames[0])).status_code == 403


def test_withdrawal_revokes_photo_access_on_next_request(team, illustrated):
    result = post(team.clients['reviewer'], illustrated.draft.url + '/withdraw-publication',
                  {'reason': 'Уточняем фотографии инструкции.', 'current_version_id': illustrated.version['id']},
                  key='withdraw-photos')
    assert result.status_code == 200, result.json()
    assert team.clients['reader'].get(image_url(team, illustrated.frames[0])).status_code == 403


@pytest.mark.parametrize('unavailable', ['archived', 'purged', 'missing_file', 'membership', 'suspended'])
def test_photo_access_rechecks_current_availability(team, illustrated, unavailable):
    if unavailable == 'archived':
        Instruction.objects.filter(pk=illustrated.instruction.id).update(lifecycle='archived')
    elif unavailable == 'purged':
        VisualSession.objects.filter(pk=illustrated.session.id).update(frames_purged_at=timezone.now())
    elif unavailable == 'missing_file':
        services.safe_frame_path(illustrated.frames[0].storage_key).unlink()
    elif unavailable == 'membership':
        Membership.objects.filter(user=team.users['reader']).update(is_active=False)
    else:
        team.org.status = 'suspended'
        team.org.save()
    result = team.clients['reader'].get(image_url(team, illustrated.frames[0]))
    assert result.status_code in {403, 404}
    assert result['Cache-Control'] == 'private, no-store'


def test_photo_access_keeps_tenant_boundary(team, illustrated):
    url = image_url(team, illustrated.frames[0])
    assert team.outsider.get(url).status_code == 404
    assert team.outsider.get(url.replace(str(team.org.id), str(team.other.id))).status_code == 404


def test_old_snapshot_read_does_not_add_metadata(team, illustrated):
    version = replace_snapshot(team, illustrated, lambda snapshot: snapshot['steps'][0].pop('evidence_frames'))
    frozen = copy.deepcopy(version.snapshot)
    result = team.clients['reader'].get(illustrated.draft.url)
    assert result.json()['publication']['snapshot'] == frozen
    assert 'evidence_frames' not in result.json()['publication']['snapshot']['steps'][0]
    assert team.clients['reader'].get(image_url(team, illustrated.frames[0])).status_code == 200


def test_step_metadata_is_bounded_sorted_and_read_only(team, draft):
    session, frames = evidence(team, draft, count=15)
    step = WorkingStep.objects.get(pk=draft.working['steps'][0]['id'])
    step.evidence_visual_session_id = session.id
    step.evidence_frame_ids = [str(frame.id) for frame in reversed(frames)] + ['bad-id']
    step.save()
    before = copy.deepcopy(step.evidence_frame_ids)
    metadata = core.step_data(step)['evidence_frames']
    assert len(metadata) == 12
    assert [item['timestamp_ms'] for item in metadata] == list(range(0, 12000, 1000))
    step.refresh_from_db()
    assert step.evidence_frame_ids == before


@pytest.mark.parametrize('mismatch', ['session', 'instruction', 'organization', 'purged'])
def test_metadata_does_not_resolve_untrusted_or_purged_links(team, draft, mismatch):
    session, frames = evidence(team, draft)
    step = WorkingStep.objects.get(pk=draft.working['steps'][0]['id'])
    step.evidence_frame_ids = [str(frame.id) for frame in frames]
    step.evidence_visual_session_id = session.id
    if mismatch == 'session':
        step.evidence_visual_session_id = uuid.uuid4()
    elif mismatch == 'instruction':
        new = post(team.clients['author'], base(team), {
            'title': 'Другая инструкция', 'purpose': 'Другая рабочая версия.', 'language': 'ru', 'demo_source': True,
        }, key='other-photo-instruction').json()
        step.working_copy_id = new['id']
    elif mismatch == 'organization':
        step.organization_id = team.other.id
    else:
        VisualSession.objects.filter(pk=session.id).update(frames_purged_at=timezone.now())
    assert core.step_data(step)['evidence_frames'] == []


@pytest.mark.parametrize('mode', ['upload', 'live'])
def test_proposal_source_comes_from_session_not_working_source(team, draft, mode):
    session, frames = evidence(team, draft, mode=mode)
    item = proposal(team, session, frames)
    expected = core.source_data(session.asset)
    # Source replacement is currently forbidden by database guards. Simulate a
    # stale relation in memory to ensure serialization stays session-bound.
    session.instruction.source_asset_id = uuid.uuid4()
    result = services.proposal_data(item)
    assert result['source'] == expected
    if mode == 'live':
        assert result['source'] is None
    else:
        assert result['source']['id'] == str(session.asset_id)
        assert result['stale'] is True
