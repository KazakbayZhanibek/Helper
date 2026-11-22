from copy import deepcopy

import pytest

from ai_drafts.providers import ProviderFailure
from ai_drafts.validation import InvalidModelOutput
from visual_drafts import demonstrations as demo
from visual_drafts import pair_verification as review
from visual_drafts import provider
from visual_drafts.teaching import present_instruction

from .test_action_protocol import images
from .test_demonstration_shared_captions import shared_answer, shared_session
from .test_frame_grounding import frames


@pytest.fixture
def candidate():
    source = images(3)
    records = frames(source)
    session = shared_session()
    wire = shared_answer()
    wire['actions'][0]['instruction'] = 'Поднимаем заготовку и с силой разглаживаем её.'
    content = demo.normalize_grounded_actions(wire, records, session)
    return source, records, session, content


def verdict(decision, title='', instruction=''):
    return {'decision': decision, 'title': title, 'instruction': instruction}


def valid_review():
    return {
        'review_version': review.VERSION,
        'language': 'ru',
        'frame_captions': {
            '1': 'Заготовка на столе.', '2': 'Рука с заготовкой над столом.', '3': 'Заготовка у подставки.',
        },
        'verdicts': {
            'o1': verdict('narrow', 'Поднимаем заготовку', 'Поднимаем заготовку над столом.'),
            'o2': verdict('keep'),
        },
    }


def test_verification_preserves_visible_part_photo_pairs_and_original(candidate):
    _, records, session, original = candidate
    original_snapshot = deepcopy(original)
    result = valid_review()
    checked = review.apply_verdicts(result, original, records, session)
    assert original == original_snapshot
    assert demo.reconcile_grounding(checked, records, session) == checked
    assert [step['evidence_frame_ids'] for step in checked['steps']] == [
        step['evidence_frame_ids'] for step in original['steps']]
    assert [step['observation_ids'] for step in checked['steps']] == [step['observation_ids'] for step in original['steps']]
    first, second = present_instruction(checked)['steps']
    assert first['description'] == 'Поднимаем заготовку над столом.'
    assert second['description'] == shared_answer()['actions'][1]['instruction']
    assert first['photo_demonstration'][1] == second['photo_demonstration'][0] == {
        'frame_id': str(records[1].id), 'caption': result['frame_captions']['2'],
    }


def test_unresolved_action_becomes_question_at_original_pair(candidate):
    _, records, session, original = candidate
    result = valid_review()
    result['verdicts']['o2'] = verdict('review', 'Выдуманный шаг', 'Выдуманная команда.')
    checked = review.apply_verdicts(result, original, records, session)
    assert len(checked['steps']) == 1
    assert any(gap['reason'] == 'unclear_action' and gap['evidence_frame_ids'] == original['steps'][1]['evidence_frame_ids']
               for gap in checked['gaps'])
    assert checked['frame_grounding']['batches'][0]['wire']['actions'][1] == {
        **original['frame_grounding']['batches'][0]['wire']['actions'][1], 'kind': 'unclear',
    }
    assert demo.reconcile_grounding(checked, records, session) == checked


@pytest.mark.parametrize('title,instruction', [
    ('  ', 'Поднимаем заготовку.'),
    ('Да', 'Поднимаем заготовку.'),
    ('Поднимаем заготовку', '  '),
    ('Поднимаем заготовку', 'Подъёма не видно.'),
    ('Возможно подъём', 'Поднимаем заготовку.'),
    ('Поднимаем заготовку', 'Поднимаем заготовку. ' * 50),
])
def test_invalid_narrow_only_moves_that_action_to_review(candidate, title, instruction):
    _, records, session, original = candidate
    original_snapshot = deepcopy(original)
    result = valid_review()
    result['verdicts']['o1'] = verdict('narrow', title, instruction)
    result['verdicts']['o2'] = verdict('keep', 'Выдуманный шаг', 'Выдуманная команда. ' * 50)
    result_snapshot = deepcopy(result)
    checked = review.apply_verdicts(result, original, records, session)
    assert original == original_snapshot
    assert result == result_snapshot
    assert [step['observation_ids'] for step in checked['steps']] == [original['steps'][1]['observation_ids']]
    assert present_instruction(checked)['steps'][0]['description'] == shared_answer()['actions'][1]['instruction']
    assert any(gap['reason'] == 'unclear_action' and gap['evidence_frame_ids'] == original['steps'][0]['evidence_frame_ids']
               for gap in checked['gaps'])
    assert demo.reconcile_grounding(checked, records, session) == checked


@pytest.mark.parametrize('change,diagnostic', [
    ('missing', ('schema', 'verdicts', 'required')),
    ('extra', ('schema', 'verdicts', 'additionalProperties')),
    ('new_pair', ('schema', 'verdicts', 'additionalProperties')),
    ('missing_caption', ('schema', 'frame_captions', 'required')),
    ('extra_caption', ('schema', 'frame_captions', 'additionalProperties')),
    ('blank_caption', ('validation', 'frame_captions', 'blank_caption')),
    ('language', ('schema', 'language', 'enum')),
])
def test_review_rejects_changed_or_partial_inventory(candidate, change, diagnostic):
    _, records, session, original = candidate
    result = valid_review()
    if change == 'missing':
        result['verdicts'].pop('o1')
    elif change == 'extra':
        result['verdicts']['untrusted content'] = verdict('review')
    elif change == 'new_pair':
        result['verdicts']['o1']['frame_pair'] = '1-3'
    elif change == 'missing_caption':
        result['frame_captions'].pop('2')
    elif change == 'extra_caption':
        result['frame_captions']['4'] = 'Лишний кадр.'
    elif change == 'blank_caption':
        result['frame_captions']['1'] = '  '
    elif change == 'language':
        result['language'] = 'kk'
    with pytest.raises(InvalidModelOutput, match='VISUAL_PAIR_REVIEW_OUTPUT_INVALID') as raised:
        review.apply_verdicts(result, original, records, session)
    assert raised.value.diagnostic == dict(zip(('stage', 'field', 'reason'), diagnostic))


def test_review_ignores_unused_captions_for_real_input_frames():
    records = frames(images(4))
    session = shared_session()
    wire = shared_answer()
    wire['frame_captions']['4'] = 'Заготовка на подставке.'
    original = demo.normalize_grounded_actions(wire, records, session)
    requested = review.review_schema(review.candidate_from_content(original))
    assert set(requested['properties']['frame_captions']['properties']) == {'1', '2', '3'}
    result = valid_review()
    expected = review.apply_verdicts(result, original, records, session)
    result['frame_captions']['4'] = 'Непроверенное описание.'
    checked = review.apply_verdicts(result, original, records, session)
    assert checked == expected
    assert checked['frame_grounding']['batches'][0]['wire']['frame_captions']['4'] == wire['frame_captions']['4']


def test_request_has_exact_pinned_photos_and_zero_price(candidate, monkeypatch):
    source, _, _, original = candidate
    monkeypatch.setattr(provider, 'capabilities', lambda _: {
        'available': True, 'provider': 'openrouter', 'model': 'dots-studio/dots-3-note-preview:free',
    })
    reviewer = review.VisualPairReviewProvider(None, candidate=review.candidate_from_content(original))
    _, schema, request, body = reviewer.prepare_request(language='ru', mode='upload', frames=source)
    assert schema['properties']['verdicts']['required'] == ['o1', 'o2']
    assert [action['frame_pair'] for action in request['unverified_actions']] == ['1-2', '2-3']
    assert 'frame_captions' not in request
    assert len([item for item in body['messages'][1]['content'] if item['type'] == 'image_url']) == 3
    assert body['reasoning'] == {'enabled': False}
    assert body['provider']['allow_fallbacks'] is False
    assert set(body['provider']['max_price'].values()) == {0}
    other = [dict(frame) for frame in source]
    other[0]['id'] = images(1)[0]['id']
    with pytest.raises(ProviderFailure, match='VISUAL_PAIR_REVIEW_INPUT_INVALID'):
        reviewer.prepare_request(language='ru', mode='upload', frames=other)
