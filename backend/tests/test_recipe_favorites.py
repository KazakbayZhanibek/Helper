from types import SimpleNamespace

import pytest
from rest_framework.test import APIClient

from core.models import Membership, RecipeFavorite
from .conftest import CONFIRMATIONS, base, checked_review, document, post, save

pytestmark = pytest.mark.django_db


@pytest.fixture
def dish(team, draft):
    details = document(draft.working, title='Салат из фасоли', recipe={'ingredients': ['Фасоль'], 'servings': 2})
    working = save(team.clients['author'], draft.url, draft.working, details)
    assert working.status_code == 200
    reviewed = checked_review(team, SimpleNamespace(url=draft.url, working=working.json()))
    result = post(team.clients['reviewer'], draft.url + '/publish', {'confirmations': CONFIRMATIONS},
                  etag=reviewed['etag'], key='favorite-recipe')
    assert result.status_code == 201
    return SimpleNamespace(url=draft.url, mark=draft.url + '/favorite', version=result.json()['id'])


def test_reader_saves_and_removes_personal_favorite_idempotently(team, dish):
    reader, admin = team.clients['reader'], team.clients['admin']
    listing = base(team)
    assert reader.get(dish.url).json()['favorite'] is False
    for _ in range(2):
        assert reader.put(dish.mark).json() == {'favorite': True}
    assert RecipeFavorite.objects.count() == 1
    rows = reader.get(listing, {'favorites': '1', 'q': 'фасоль', 'page_size': 1}).json()
    assert rows['total'] == 1 and rows['items'][0]['favorite'] is True
    assert reader.get(dish.url).json()['favorite'] is True
    assert admin.get(dish.url).json()['favorite'] is False
    assert admin.get(listing, {'favorites': '1'}).json()['items'] == []
    for _ in range(2):
        assert reader.delete(dish.mark).json() == {'favorite': False}
    assert reader.get(listing, {'favorites': '1'}).json()['total'] == 0
    assert reader.get(listing).json()['items'][0]['favorite'] is False


def test_favorites_show_published_revision_and_follow_publication_access(team, dish):
    author = team.clients['author']
    assert author.put(dish.mark).status_code == 200
    working = post(author, dish.url + '/create-draft', {'source_version_id': dish.version}).json()
    edited = save(author, dish.url, working, document(working, title='Совсем другой черновик'))
    assert edited.status_code == 200
    items = author.get(base(team), {'favorites': '1', 'q':'салат'}).json()['items']
    assert [(item['title'], item['status']) for item in items] == [('Салат из фасоли', 'published')]
    assert author.get(base(team), {'favorites': '1', 'q':'другой'}).json()['items'] == []
    response = post(team.clients['admin'], dish.url + '/withdraw-publication', {
        'reason': 'Нужно проверить количество продуктов.', 'current_version_id': dish.version})
    assert response.status_code == 200
    assert author.get(base(team), {'favorites':'1'}).json()['items'] == []
    assert author.get(dish.url).json()['favorite'] is None
    assert author.put(dish.mark).status_code == 409


def test_favorites_require_current_kitchen_access(team, dish):
    assert APIClient().put(dish.mark).status_code == 401
    assert team.outsider.put(dish.mark).status_code == 404
    assert team.outsider.delete(dish.mark).status_code == 404
    assert team.clients['reader'].put(dish.mark).status_code == 200
    Membership.objects.filter(user=team.users['reader'], organization=team.org).update(is_active=False)
    assert team.clients['reader'].get(base(team), {'favorites':'1'}).status_code == 404
    assert team.clients['reader'].put(dish.mark).status_code == 404


def test_legacy_materials_cannot_be_favorited(team, publication):
    assert team.clients['admin'].put(publication.url + '/favorite').status_code == 409
    assert team.clients['reader'].get(publication.url).json()['favorite'] is None


def test_unpublished_recipe_is_private_and_cannot_be_favorited(team, draft):
    working = document(draft.working, recipe={'ingredients': ['Мука'], 'servings': None})
    assert save(team.clients['author'], draft.url, draft.working, working).status_code == 200
    assert team.clients['author'].put(draft.url + '/favorite').status_code == 409
    assert team.clients['reader'].put(draft.url + '/favorite').status_code == 404
    assert not RecipeFavorite.objects.exists()
