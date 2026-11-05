"""Cross-feature regressions: provenance survives editing and media stays private."""
import uuid

import pytest
from django.db import IntegrityError, transaction

from core.models import MediaAsset, WorkingStep
from .conftest import document, post, save


@pytest.mark.django_db
def test_ai_provenance_survives_edit_and_cannot_be_forged(team, draft):
    step = WorkingStep.objects.get(id=draft.working['steps'][0]['id'])
    evidence = [str(uuid.uuid4())]
    step.origin = 'ai'
    step.evidence_segment_ids = evidence
    step.evidence_transcript_revision = 2
    step.warning_codes = ['missing_parameters']
    step.save()
    current = team.clients['author'].get(draft.url + '/working-copy').json()
    payload = document(current)
    payload['steps'][0]['description'] = 'Сверьте предметы по карточке комплекта.'
    response = save(team.clients['author'], draft.url, current, payload)
    assert response.status_code == 200
    edited = response.json()
    first = edited['steps'][0]
    assert first['origin'] == 'ai_edited'
    assert first['evidence_segment_ids'] == evidence
    assert first['evidence_transcript_revision'] == 2
    assert first['warning_codes'] == ['missing_parameters']
    assert first['checked'] is False
    forged = document(edited)
    forged['steps'][0]['evidence_segment_ids'] = [str(uuid.uuid4())]
    assert save(team.clients['author'], draft.url, edited, forged).status_code == 400


@pytest.mark.django_db
def test_visual_provenance_survives_manual_edits_and_rejects_substitution(team, draft):
    step = WorkingStep.objects.get(id=draft.working['steps'][0]['id'])
    frames = [str(uuid.uuid4()), str(uuid.uuid4())]
    visual_session = uuid.uuid4()
    step.origin = 'ai'
    step.evidence_frame_ids = frames
    step.evidence_visual_session_id = visual_session
    step.warning_codes = ['approximate_timing']
    step.save()
    current = team.clients['author'].get(draft.url + '/working-copy').json()
    payload = document(current)
    payload['steps'][0]['title'] = 'Сверить состав набора'
    response = save(team.clients['author'], draft.url, current, payload)
    assert response.status_code == 200
    edited = response.json()
    first = edited['steps'][0]
    assert first['origin'] == 'ai_edited'
    assert first['evidence_frame_ids'] == frames
    assert first['evidence_visual_session_id'] == str(visual_session)
    assert first['warning_codes'] == ['approximate_timing']
    forged = document(edited)
    forged['steps'][0]['evidence_frame_ids'] = [str(uuid.uuid4())]
    assert save(team.clients['author'], draft.url, edited, forged).status_code == 400


@pytest.mark.django_db
def test_media_prepare_can_assign_derivatives_once(team):
    asset = MediaAsset.objects.create(organization=team.org, status='quarantined', is_demo=False)
    MediaAsset.objects.filter(pk=asset.pk).update(duration_ms=12000, storage_key='private/ready.mp4', audio_key='private/audio.wav', thumbnail_key='private/poster.jpg', status='ready', has_audio=True)
    with pytest.raises(IntegrityError), transaction.atomic():
        MediaAsset.objects.filter(pk=asset.pk).update(duration_ms=13000)
    with pytest.raises(IntegrityError), transaction.atomic():
        MediaAsset.objects.filter(pk=asset.pk).update(thumbnail_key='other.jpg')
    # Toggling status does not make prepared content mutable again.
    MediaAsset.objects.filter(pk=asset.pk).update(status='rejected')
    with pytest.raises(IntegrityError), transaction.atomic():
        MediaAsset.objects.filter(pk=asset.pk).update(storage_key='replacement.mp4')


@pytest.mark.django_db
def test_unprepared_media_allows_manual_draft_without_null_duration_crash(team, settings):
    response = post(team.clients['author'], f'/api/v1/orgs/{team.org.id}/instructions', {'title': 'Проверка нового видео', 'purpose': 'Подготовить шаги до окончания обработки.', 'language': 'ru', 'demo_source': False}, key=str(uuid.uuid4()))
    working = response.json()
    asset = MediaAsset.objects.create(organization=team.org, status='quarantined', is_demo=False, original_key='quarantine/source.mp4')
    instruction = team.org.instruction_set.get(id=working['instruction_id'])
    instruction.source_asset = asset
    instruction.save()
    url = f'/api/v1/orgs/{team.org.id}/instructions/{instruction.id}'
    payload = document(working, steps=[{'id': None, 'title': 'Проверить набор', 'description': 'Сверьте состав по карточке.', 'start_ms': 0, 'end_ms': 5000, 'evidence_segment_ids': []}])
    response = save(team.clients['author'], url, working, payload)
    assert response.status_code == 200
    source = response.json()['source']
    assert source['is_demo'] is False
    assert source['playback_url'] is None
    assert 'original_key' not in source
    assert team.clients['author'].get(f'/api/v1/orgs/{team.org.id}/assets/{asset.id}/playback').status_code == 404
    assert team.clients['author'].get(f'/api/v1/orgs/{team.org.id}/assets/{asset.id}/thumbnail').status_code == 404


@pytest.mark.django_db
def test_private_thumbnail_requires_same_source_access(team, settings):
    from core.models import Instruction, WorkingCopy
    asset = MediaAsset.objects.create(organization=team.org, is_demo=False, storage_key='ready.mp4', duration_ms=12000, thumbnail_key='poster.jpg')
    instruction = Instruction.objects.create(organization=team.org, owner=team.users['author'], source_asset=asset)
    WorkingCopy.objects.create(organization=team.org, instruction=instruction, title='Учебное видео', purpose='Проверка доступа к обложке.')
    (settings.PRIVATE_MEDIA_ROOT / 'poster.jpg').write_bytes(b'private-jpeg-test')
    url = f'/api/v1/orgs/{team.org.id}/assets/{asset.id}/thumbnail'
    response = team.clients['author'].get(url)
    assert response.status_code == 200
    assert response['Content-Type'] == 'image/jpeg'
    assert b''.join(response.streaming_content) == b'private-jpeg-test'
    assert team.clients['reader'].get(url).status_code == 404
    assert team.outsider.get(url).status_code == 404
