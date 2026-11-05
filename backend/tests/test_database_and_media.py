import pytest
from django.db import DatabaseError, transaction

from core.models import Instruction, MediaAsset, PublishedVersion, WorkingCopy, WorkingStep

pytestmark = pytest.mark.django_db


def test_database_rejects_cross_tenant_relations(team, draft):
    other_asset = MediaAsset.objects.create(organization=team.other, storage_key='demo-process.mp4', duration_ms=60000)
    with pytest.raises(DatabaseError), transaction.atomic():
        Instruction.objects.filter(id=draft.working['instruction_id']).update(source_asset=other_asset)
    with pytest.raises(DatabaseError), transaction.atomic():
        WorkingCopy.objects.filter(id=draft.working['id']).update(organization=team.other)
    with pytest.raises(DatabaseError), transaction.atomic():
        WorkingStep.objects.filter(id=draft.working['steps'][0]['id']).update(organization=team.other)


def test_database_rejects_publication_mutation_and_foreign_current_version(team, publication):
    version_id = publication.version['id']
    with pytest.raises(DatabaseError), transaction.atomic():
        PublishedVersion.objects.filter(id=version_id).update(snapshot={'title': 'Changed via queryset'})
    foreign_instruction = Instruction.objects.create(organization=team.other, owner=team.users['author'])
    with pytest.raises(DatabaseError), transaction.atomic():
        Instruction.objects.filter(id=foreign_instruction.id).update(current_version_id=version_id)
    same_org_instruction = Instruction.objects.create(organization=team.org, owner=team.users['author'])
    with pytest.raises(DatabaseError), transaction.atomic():
        Instruction.objects.filter(id=same_org_instruction.id).update(current_version_id=version_id)


def test_protected_playback_supports_exact_ranges_head_and_invalid_range(team, publication):
    client = team.clients['reader']
    url = publication.version['snapshot']['source']['playback_url']
    response = client.get(url, HTTP_RANGE='bytes=100-199')
    assert response.status_code == 206
    assert response['Content-Range'] == 'bytes 100-199/2560'
    assert response['Content-Length'] == '100'
    assert response['Content-Type'] == 'video/mp4'
    assert response['Cache-Control'] == 'private, no-store'
    assert b''.join(response.streaming_content) == bytes(range(100, 200))
    suffix = client.get(url, HTTP_RANGE='bytes=-2')
    assert suffix.status_code == 206
    assert b''.join(suffix.streaming_content) == bytes([254, 255])
    head = client.head(url)
    assert head.status_code == 200
    assert head['Content-Length'] == '2560'
    assert b''.join(head.streaming_content) == b''
    for byte_range in ('bytes=2560-', 'bytes=30-20', 'bytes=-0', 'bytes=0-10,20-30', 'not-a-range', 'bytes=' + '1' * 4400 + '-'):
        invalid = client.get(url, HTTP_RANGE=byte_range)
        assert invalid.status_code == 416
        assert invalid['Content-Range'] == 'bytes */2560'


def test_storage_key_cannot_escape_private_root(team, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-outside.mp4')
    outside.write_bytes(b'private file outside the media root')
    asset = MediaAsset.objects.create(organization=team.org, storage_key='../' + outside.name, duration_ms=60000)
    Instruction.objects.create(organization=team.org, owner=team.users['author'], source_asset=asset)
    url = f'/api/v1/orgs/{team.org.id}/assets/{asset.id}/playback'
    assert team.clients['author'].get(url).status_code == 404
