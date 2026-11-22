from types import SimpleNamespace

import pytest
from core.models import Instruction, WorkingCopy

from .conftest import CONFIRMATIONS, base, checked_review, document, post, save


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


EMPTY_RECIPE = {'ingredients': [], 'servings': None}


def new_recipe(team, recipe):
    return post(team.clients['author'], base(team), {
        'title': 'Тёплый салат', 'purpose': 'Салат для семейного обеда.', 'language': 'ru',
        'recipe': recipe, 'demo_source': True,
    }, key='create-salad')


def test_new_recipe_requires_ingredients_even_after_clear_by_old_editor(team):
    author = team.clients['author']
    created = new_recipe(team, EMPTY_RECIPE)
    assert created.status_code == 201, created.data
    working = created.json()
    url = base(team) + '/' + working['instruction_id']
    assert working['recipe'] == EMPTY_RECIPE
    step = {'title': 'Нарежьте овощи', 'description': 'Нарежьте огурец ломтиками.',
            'start_ms': 0, 'end_ms': 1000}
    saved = save(author, url, working, document(working, recipe=None, steps=[step]))
    assert saved.status_code == 200, saved.data
    assert saved.json()['recipe'] == EMPTY_RECIPE
    reviewed = post(author, url + '/submit-review', etag=saved.json()['etag'])
    assert reviewed.status_code == 400
    assert 'recipe.ingredients' in reviewed.json()['error']['field_errors']
    assert author.get(url).json()['kind'] == 'recipe'


def test_invalid_recipe_create_is_atomic(team):
    response = new_recipe(team, {'ingredients': ['Лук'], 'servings': 0})
    assert response.status_code == 400
    assert 'recipe.servings' in response.json()['error']['field_errors']
    assert not Instruction.objects.exists()
    assert new_recipe(team, EMPTY_RECIPE).status_code == 201


def test_library_uses_visible_version_for_recipe_kind_counts_and_ingredient_search(team, publication):
    author, reader = (team.clients[role] for role in ('author', 'reader'))
    listing = base(team)
    legacy_version = publication.version
    working = post(author, publication.url + '/create-draft', {'source_version_id': legacy_version['id']}).json()
    recipe = {'ingredients': ['Йогурт', 'Тёртая корица — щепотка'], 'servings': 3}
    edited = save(author, publication.url, working, document(working, recipe=recipe)).json()

    def items(client, **query):
        response = client.get(listing, query)
        assert response.status_code == 200, response.data
        return response.json()['items']

    assert items(reader, kind='recipe') == []
    assert items(reader, kind='legacy')[0]['kind'] == 'legacy'
    assert items(author, kind='legacy') == []
    found = items(author, kind='recipe', q='КОРИЦА')
    assert len(found) == 1 and found[0]['ingredient_count'] == 2 and found[0]['servings'] == 3
    assert items(reader, q='корица') == []
    reviewed = checked_review(team, SimpleNamespace(url=publication.url, working=edited))
    published = post(team.clients['reviewer'], publication.url + '/publish',
                     {'confirmations': CONFIRMATIONS}, etag=reviewed['etag'], key='publish-ingredients')
    assert published.status_code == 201, published.data
    now_visible = items(reader, kind='recipe', q='И\u0306огурт')
    assert len(now_visible) == 1 and now_visible[0]['ingredient_count'] == 2
    assert items(reader, q='тертая')[0]['id'] == edited['instruction_id']
    assert items(reader, kind='legacy') == []
    assert items(reader, kind='recipe', status='draft') == []

    # Returning to an older draft affects the author but leaves the published recipe visible.
    reopened = post(author, publication.url + '/create-draft', {'source_version_id': legacy_version['id']})
    assert reopened.status_code == 201
    assert items(author, kind='legacy')[0]['ingredient_count'] == 0
    assert items(reader, kind='recipe')[0]['servings'] == 3


def test_kind_filter_applies_before_pagination(team, draft):
    assert new_recipe(team, EMPTY_RECIPE).status_code == 201
    author = team.clients['author']
    for kind, wanted_id in [('recipe', None), ('legacy', draft.working['instruction_id'])]:
        first = author.get(base(team), {'kind': kind, 'page_size': 1}).json()
        assert first['total'] == 1 and first['next_cursor'] is None
        assert first['items'][0]['kind'] == kind
        if wanted_id:
            assert first['items'][0]['id'] == wanted_id
        assert author.get(base(team), {'kind': kind, 'page_size': 1, 'cursor': 1}).json()['items'] == []
    assert author.get(base(team), {'kind': 'typo'}).status_code == 400
