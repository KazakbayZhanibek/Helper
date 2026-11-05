import io
import uuid
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Instruction, Membership, MediaAsset
from media_pipeline.models import MediaQuota, UploadSession, ProcessingJob
from media_pipeline.uploads import receive_upload, MAX_SIZE
from .conftest import base, post

pytestmark = pytest.mark.django_db


def new_instruction(team):
    response = post(team.clients['author'], base(team), {'title': 'Реальный процесс', 'purpose': 'Показать сборку', 'language': 'ru'}, key=str(uuid.uuid4()))
    assert response.status_code == 201, response.data
    return response.json()['instruction_id']


def start(team, instruction=None, size=8, **overrides):
    instruction = instruction or new_instruction(team)
    data = {'filename': 'process.mp4', 'size_bytes': size, 'language': 'ru', 'rights_confirmed': True, **overrides}
    response = post(team.clients['author'], base(team) + f'/{instruction}/uploads', data, key=str(uuid.uuid4()))
    return response


def upload_url(team, upload_id):
    return f'/api/v1/orgs/{team.org.id}/uploads/{upload_id}'


def test_upload_attaches_private_quarantine_and_idempotent_completion(team, settings):
    instruction_id = new_instruction(team)
    url = base(team) + f'/{instruction_id}/uploads'
    payload = {'filename': '../../unsafe.mp4', 'size_bytes': 8, 'language': 'ru', 'rights_confirmed': True}
    first = post(team.clients['author'], url, payload, key='same-upload')
    repeated = post(team.clients['author'], url, payload, key='same-upload')
    assert first.status_code == repeated.status_code == 201
    assert first.json() == repeated.json()
    upload_id = first.json()['id']
    content = upload_url(team, upload_id) + '/content'
    response = team.clients['author'].post(content, b'notvideo', content_type='application/octet-stream')
    assert response.status_code == 202, response.data
    again = team.clients['author'].post(content, b'notvideo', content_type='application/octet-stream')
    assert again.status_code == 202 and again.json() == response.json()
    instruction = Instruction.objects.get(pk=instruction_id)
    asset = instruction.source_asset
    assert asset.status == 'quarantined' and asset.duration_ms is None and not asset.storage_key
    assert asset.original_size_bytes == 8 and len(asset.sha256) == 64 and not asset.is_demo
    assert '..' not in asset.original_key and 'unsafe' not in asset.original_key
    assert (settings.PRIVATE_MEDIA_ROOT / asset.original_key).read_bytes() == b'notvideo'
    assert instruction.working_copy.revision == 2
    quota = MediaQuota.objects.get(organization=team.org)
    assert quota.reserved_bytes == 0 and quota.used_bytes == 8
    assert ProcessingJob.objects.filter(instruction=instruction).count() == 1
    playback = team.clients['author'].get(f'/api/v1/orgs/{team.org.id}/assets/{asset.id}/playback')
    assert playback.status_code == 404
    assert team.clients['reader'].get(base(team) + f'/{instruction_id}/jobs').status_code == 403
    assert team.outsider.get(base(team) + f'/{instruction_id}/jobs').status_code == 404


def test_upload_refuses_wrong_tenant_role_and_missing_rights(team):
    instruction = new_instruction(team)
    url = base(team) + f'/{instruction}/uploads'
    data = {'filename': 'a.mp4', 'size_bytes': 8, 'rights_confirmed': True}
    assert post(team.clients['reader'], url, data, key='reader').status_code == 403
    assert post(team.outsider, url, data, key='foreign').status_code == 404
    assert start(team, instruction, rights_confirmed=False).status_code == 400
    assert start(team, instruction, size=MAX_SIZE + 1).status_code == 413
    assert not UploadSession.objects.exists()


def test_raw_upload_enforces_csrf_before_reading_body(team):
    result = start(team)
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(team.users['author'])
    response = client.post(upload_url(team, result.json()['id']) + '/content', b'notvideo', content_type='application/octet-stream')
    assert response.status_code == 403
    assert response.json()['error']['code'] == 'CSRF_FAILED'
    assert UploadSession.objects.get(pk=result.json()['id']).state == 'pending'


def test_raw_upload_with_real_csrf_cookie_and_header_succeeds(team):
    result = start(team)
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(team.users['author'])
    csrf = client.get('/api/v1/auth/csrf')
    assert csrf.status_code == 200 and 'csrftoken' in client.cookies
    response = client.post(upload_url(team, result.json()['id']) + '/content', b'notvideo',
        content_type='application/octet-stream', HTTP_X_CSRFTOKEN=csrf.json()['csrf_token'])
    assert response.status_code == 202, response.data
    assert UploadSession.objects.get(pk=result.json()['id']).state == 'completed'


def test_quota_reservations_cancel_once_and_expire(team):
    MediaQuota.objects.create(organization=team.org, limit_bytes=10)
    first = start(team)
    assert first.status_code == 201
    assert start(team).status_code == 413
    url = upload_url(team, first.json()['id'])
    assert team.clients['author'].delete(url).status_code == 204
    assert team.clients['author'].delete(url).status_code == 204
    assert MediaQuota.objects.get(organization=team.org).reserved_bytes == 0
    second = start(team)
    UploadSession.objects.filter(pk=second.json()['id']).update(expires_at=timezone.now() - timedelta(seconds=1))
    third = start(team)
    assert third.status_code == 201
    assert UploadSession.objects.get(pk=second.json()['id']).state == 'expired'
    assert MediaQuota.objects.get(organization=team.org).reserved_bytes == 8


@pytest.mark.parametrize('body', [b'short', b'too-many-bytes'])
def test_actual_bytes_verified_and_reservation_released(team, settings, body):
    result = start(team)
    member = Membership.objects.get(organization=team.org, user=team.users['author'])
    from core.http import DomainError
    with pytest.raises(DomainError):
        receive_upload(member, result.json()['id'], io.BytesIO(body))
    assert UploadSession.objects.get(pk=result.json()['id']).state == 'failed'
    assert MediaQuota.objects.get(organization=team.org).reserved_bytes == 0
    assert not MediaAsset.objects.exists()
    assert not list(settings.PRIVATE_MEDIA_ROOT.glob('quarantine/**/*.original'))


def test_upload_session_requires_its_uploader_or_admin(team):
    result = start(team)
    url = upload_url(team, result.json()['id'])
    assert team.clients['reviewer'].delete(url).status_code == 404
    assert team.clients['admin'].delete(url).status_code == 204


def test_periodic_cleanup_releases_abandoned_reservation(team):
    from media_pipeline.maintenance import maintenance_tick
    result = start(team)
    UploadSession.objects.filter(pk=result.json()['id']).update(expires_at=timezone.now() - timedelta(seconds=1))
    maintenance_tick()
    assert MediaQuota.objects.get(organization=team.org).reserved_bytes == 0
    assert UploadSession.objects.get(pk=result.json()['id']).state == 'expired'


def test_upload_db_guard_rejects_cross_tenant_write(team):
    result = start(team)
    with pytest.raises(IntegrityError), transaction.atomic():
        UploadSession.objects.filter(pk=result.json()['id']).update(organization=team.other)


def test_cancellation_retry_and_tenant_job_access(team):
    result = start(team)
    response = team.clients['author'].post(upload_url(team, result.json()['id']) + '/content', b'notvideo', content_type='application/octet-stream')
    job_id = response.json()['job_id']
    job_url = f'/api/v1/orgs/{team.org.id}/jobs/{job_id}'
    assert team.outsider.get(job_url).status_code == 404
    cancelled = post(team.clients['author'], job_url + '/cancel')
    assert cancelled.status_code == 202 and cancelled.json()['state'] == 'cancelled'
    retry = post(team.clients['author'], job_url + '/retry', key='retry1')
    assert retry.status_code == 202 and retry.json()['state'] == 'queued'
    assert post(team.clients['author'], job_url + '/retry', key='retry1').json() == retry.json()
    assert post(team.clients['author'], job_url + '/retry', key='retry2').status_code == 409
