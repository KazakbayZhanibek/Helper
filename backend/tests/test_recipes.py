from types import SimpleNamespace

import pytest
from core.models import WorkingCopy

from .conftest import CONFIRMATIONS, checked_review, document, post, save


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('empty_recipe', [None, {'ingredients': [], 'servings': None}])
def test_recipe_can_be_saved_preserved_by_old_client_and_removed(team, draft, empty_recipe):
    author = team.clients['author']
    assert draft.working['recipe'] is None
    saved = save(author, draft.url, draft.working, document(draft.working, recipe={
        'ingredients': ['  Мука — 400 г ', 'И\u0306огурт — 250 мл'], 'servings': 4,
    }))
    assert saved.status_code == 200, saved.data
    recipe = {'ingredients': ['Мука — 400 г', 'Йогурт — 250 мл'], 'servings': 4}
    assert saved.json()['recipe'] == recipe
    retained = save(author, draft.url, saved.json())
    assert retained.status_code == 200, retained.data
    assert retained.json()['recipe'] == recipe
    removed = save(author, draft.url, retained.json(), document(retained.json(), recipe=empty_recipe))
    assert removed.status_code == 200, removed.data
    assert removed.json()['recipe'] is None


@pytest.mark.parametrize('recipe', [
    {}, {'ingredients': [], 'servings': None, 'source': 'ai'},
    {'ingredients': 'Мука', 'servings': None},
    {'ingredients': ['Мука'] * 31, 'servings': None},
    {'ingredients': [False], 'servings': None},
    {'ingredients': [' '], 'servings': None},
    {'ingredients': ['а' * 161], 'servings': None},
    {'ingredients': ['Мука\nВода'], 'servings': None},
    {'ingredients': ['Мука\x00'], 'servings': None},
    *[{'ingredients': ['Мука'], 'servings': invalid} for invalid in (True, 0, 1001, '4', 4.0)],
])
def test_invalid_recipe_does_not_change_working_copy(team, draft, recipe):
    author = team.clients['author']
    response = save(author, draft.url, draft.working, document(draft.working, recipe=recipe))
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'VALIDATION_ERROR'
    assert author.get(draft.url + '/working-copy').json() == draft.working


def test_incomplete_recipe_can_be_saved_but_requires_ingredients_for_review(team, draft):
    author = team.clients['author']
    response = save(author, draft.url, draft.working, document(draft.working, recipe={
        'ingredients': [], 'servings': 2,
    }))
    assert response.status_code == 200, response.data
    working = response.json()
    blocked = post(author, draft.url + '/submit-review', etag=working['etag'])
    assert blocked.status_code == 400
    assert 'recipe.ingredients' in blocked.json()['error']['field_errors']
    assert author.get(draft.url + '/working-copy').json() == working


def test_recipe_is_published_and_restored_with_its_selected_version(team, publication):
    author, reviewer, reader = (team.clients[role] for role in ('author', 'reviewer', 'reader'))
    legacy = publication.version
    assert 'recipe' not in legacy['snapshot']
    working = post(author, publication.url + '/create-draft', {'source_version_id': legacy['id']}).json()
    recipe = {'ingredients': ['Тесто — 400 г', 'Мука для стола'], 'servings': None}
    edited = save(author, publication.url, working, document(working, recipe=recipe))
    assert edited.status_code == 200, edited.data
    before_publish = reader.get(publication.url).json()
    assert before_publish['working_copy'] is None
    assert before_publish['publication']['snapshot'] == legacy['snapshot']
    reviewed = checked_review(team, SimpleNamespace(url=publication.url, working=edited.json()))
    response = post(reviewer, publication.url + '/publish', {'confirmations': CONFIRMATIONS},
                    etag=reviewed['etag'], key='publish-recipe')
    assert response.status_code == 201, response.data
    version = response.json()
    assert version['snapshot']['recipe'] == recipe
    assert reader.get(publication.url).json()['publication']['snapshot']['recipe'] == recipe
    restored_recipe = post(author, publication.url + '/create-draft', {'source_version_id': version['id']})
    assert restored_recipe.status_code == 201, restored_recipe.data
    assert restored_recipe.json()['recipe'] == recipe


def test_reopening_legacy_version_clears_recipe_from_newer_working_copy(team, publication):
    WorkingCopy.objects.filter(id=publication.working['id']).update(recipe={
        'ingredients': ['Данные более новой версии'], 'servings': 5,
    })
    restored = post(team.clients['author'], publication.url + '/create-draft', {
        'source_version_id': publication.version['id'],
    })
    assert restored.status_code == 201, restored.data
    assert restored.json()['recipe'] is None
