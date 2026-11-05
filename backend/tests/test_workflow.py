import copy
import uuid

import pytest
from django.db import IntegrityError, transaction
from core.models import Instruction, MediaAsset, PublishedVersion, WorkingCopy, WorkingStep
from .conftest import CONFIRMATIONS, base, checked_review, document, post, save

pytestmark = pytest.mark.django_db


def test_if_match_required_and_stale_cannot_overwrite(team, draft):
    client = team.clients['author']
    original = draft.working
    response = client.put(draft.url + '/working-copy', document(original), format='json')
    assert response.status_code == 428
    assert response.json()['error']['code'] == 'PRECONDITION_REQUIRED'
    updated = save(client, draft.url, original, document(original, title='Новый заголовок инструкции'))
    assert updated.status_code == 200
    assert updated['ETag'] != original['etag']
    stale = save(client, draft.url, original, document(original, title='Устаревшее изменение'))
    assert stale.status_code == 412
    assert stale.json()['error']['details']['actual_revision'] == updated.json()['revision']
    assert client.get(draft.url + '/working-copy').json()['title'] == 'Новый заголовок инструкции'


@pytest.mark.parametrize('start,end', [(True, 1000), (0, False), (0.5, 1000), (-1, 1000), (1000, 1000), (2000, 1000), (0, 60001)])
def test_invalid_times_are_rejected_atomically(team, draft, start, end):
    payload = document(draft.working)
    payload['steps'][0].update(start_ms=start, end_ms=end)
    response = save(team.clients['author'], draft.url, draft.working, payload)
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'INVALID_TIME_RANGE'
    assert team.clients['author'].get(draft.url + '/working-copy').json() == draft.working


def test_reordering_keeps_ids_and_rejects_duplicate_or_foreign_steps(team, draft):
    original = draft.working
    payload = document(original)
    payload['steps'].reverse()
    response = save(team.clients['author'], draft.url, original, payload)
    assert response.status_code == 200
    result = response.json()
    assert [step['id'] for step in result['steps']] == [step['id'] for step in reversed(original['steps'])]
    assert [step['ordinal'] for step in result['steps']] == [1, 2]
    for invalid_id in (result['steps'][0]['id'], str(uuid.uuid4())):
        payload = document(result)
        payload['steps'][1]['id'] = invalid_id
        assert save(team.clients['author'], draft.url, result, payload).status_code == 400


def test_client_cannot_forge_review_or_provenance(team, draft):
    payload = document(draft.working)
    payload['steps'][0]['checked_by'] = str(team.users['reviewer'].id)
    assert save(team.clients['author'], draft.url, draft.working, payload).status_code == 400
    del payload['steps'][0]['checked_by']
    payload['steps'][0]['origin'] = 'ai'
    assert save(team.clients['author'], draft.url, draft.working, payload).status_code == 400


@pytest.mark.parametrize('language', [[], {}, 42, None])
def test_malformed_language_returns_validation_error(team, draft, language):
    response = save(team.clients['author'], draft.url, draft.working, document(draft.working, language=language))
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'VALIDATION_ERROR'


def test_huge_timestamps_without_source_fail_before_database_write(team):
    response = post(team.clients['author'], base(team), {'title': 'Инструкция без источника', 'purpose': 'Добавить источник следующим этапом.', 'language': 'ru'}, key='no-source')
    assert response.status_code == 201
    working = response.json()
    url = base(team) + '/' + working['instruction_id']
    payload = document(working, steps=[{'id': None, 'title': 'Первый шаг', 'description': 'Пояснение первого шага.', 'start_ms': 0, 'end_ms': 10 ** 100, 'evidence_segment_ids': []}])
    denied = save(team.clients['author'], url, working, payload)
    assert denied.status_code == 400
    assert denied.json()['error']['code'] == 'INVALID_TIME_RANGE'
    assert team.clients['author'].get(url + '/working-copy').json() == working


def test_review_locks_editing_and_return_clears_checks(team, draft):
    working = checked_review(team, draft)
    assert all(step['checked'] for step in working['steps'])
    assert save(team.clients['author'], draft.url, working).status_code == 400
    assert post(team.clients['reviewer'], draft.url + '/return-to-draft', {'reason': 'коротко'}, etag=working['etag']).status_code == 400
    returned = post(team.clients['reviewer'], draft.url + '/return-to-draft', {'reason': 'Уточните порядок второго шага.'}, etag=working['etag'])
    assert returned.status_code == 200
    assert returned.json()['editable'] is True
    assert all(not step['checked'] for step in returned.json()['steps'])
    assert returned.json()['return_reason'] == 'Уточните порядок второго шага.'


def test_publish_requires_review_checks_and_exact_confirmations(team, draft):
    reviewer = team.clients['reviewer']
    body = {'confirmations': CONFIRMATIONS}
    assert post(reviewer, draft.url + '/publish', body, etag=draft.working['etag'], key='too-early').status_code == 400
    working = post(team.clients['author'], draft.url + '/submit-review', etag=draft.working['etag']).json()
    assert post(reviewer, draft.url + '/publish', body, etag=working['etag'], key='unchecked').status_code == 400
    for step in working['steps']:
        working = post(reviewer, draft.url + '/review/check-step', {'step_id': step['id'], 'checked': True}, etag=working['etag']).json()
    for value in (None, {}, {**CONFIRMATIONS, 'privacy': 1}, {**CONFIRMATIONS, 'privacy': False}):
        assert post(reviewer, draft.url + '/publish', {'confirmations': value}, etag=working['etag'], key=str(uuid.uuid4())).status_code == 400
    assert PublishedVersion.objects.count() == 0


def test_ready_source_required(team, draft):
    MediaAsset.objects.filter(id=draft.working['source']['id']).update(status='rejected')
    assert post(team.clients['author'], draft.url + '/submit-review', etag=draft.working['etag']).status_code == 400
    assert WorkingCopy.objects.get(id=draft.working['id']).workflow == 'draft'


def test_create_is_idempotent_and_key_payload_bound(team):
    client = team.clients['author']
    payload = {'title': 'Тестовая инструкция', 'purpose': 'Проверить повтор запроса создания.', 'language': 'ru', 'demo_source': True}
    first = post(client, base(team), payload, key='create-once')
    second = post(client, base(team), payload, key='create-once')
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert Instruction.objects.count() == 1
    changed = post(client, base(team), {**payload, 'title': 'Другое содержимое'}, key='create-once')
    assert changed.status_code == 409
    assert changed.json()['error']['code'] == 'IDEMPOTENCY_KEY_REUSED'


def test_publish_is_idempotent_and_snapshot_remains_immutable(team, draft):
    working = checked_review(team, draft)
    payload = {'confirmations': CONFIRMATIONS}
    first = post(team.clients['reviewer'], draft.url + '/publish', payload, etag=working['etag'], key='publish-once')
    replay = post(team.clients['reviewer'], draft.url + '/publish', payload, etag=working['etag'], key='publish-once')
    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert PublishedVersion.objects.count() == 1
    changed_replay = post(team.clients['reviewer'], draft.url + '/publish', {'confirmations': {**CONFIRMATIONS, 'privacy': False}}, etag=working['etag'], key='publish-once')
    assert changed_replay.status_code == 409
    assert changed_replay.json()['error']['code'] == 'IDEMPOTENCY_KEY_REUSED'
    frozen = copy.deepcopy(first.json()['snapshot'])
    reopened = post(team.clients['author'], draft.url + '/create-draft', {'source_version_id': first.json()['id']})
    assert reopened.status_code == 201
    assert all(not step['checked'] for step in reopened.json()['steps'])
    edited = save(team.clients['author'], draft.url, reopened.json(), document(reopened.json(), title='СЕКРЕТНЫЙ НОВЫЙ ЧЕРНОВИК'))
    assert edited.status_code == 200
    reader_result = team.clients['reader'].get(draft.url).json()
    assert reader_result['publication']['snapshot'] == frozen
    assert 'СЕКРЕТНЫЙ' not in str(reader_result)
    assert team.clients['reader'].get(base(team) + '?q=СЕКРЕТНЫЙ').json()['items'] == []
    assert PublishedVersion.objects.get(id=first.json()['id']).snapshot == frozen
    assert post(team.clients['author'], draft.url + '/create-draft', {'source_version_id': first.json()['id']}).status_code == 409


def test_withdraw_hides_reader_search_detail_and_media(team, publication):
    response = post(team.clients['reviewer'], publication.url + '/withdraw-publication', {'reason': 'Найдена ошибка в порядке действий.', 'current_version_id': publication.version['id']})
    assert response.status_code == 200
    reader = team.clients['reader']
    assert reader.get(publication.url).status_code == 404
    assert reader.get(base(team)).json()['items'] == []
    assert reader.get(publication.version['snapshot']['source']['playback_url']).status_code == 404
    assert PublishedVersion.objects.count() == 1
