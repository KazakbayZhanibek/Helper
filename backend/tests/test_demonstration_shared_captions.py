import pytest
from jsonschema import Draft202012Validator

from ai_drafts.validation import InvalidModelOutput, parse_json
from visual_drafts import demonstrations as demo
from visual_drafts.teaching import present_instruction
from .test_action_protocol import images
from .test_demonstrations import selected
from .test_frame_grounding import frames


def shared_session():
    session = selected()
    session.analysis_budget.update(demonstration_wire=demo.SHARED_CAPTIONS_WIRE_VERSION,
                                   action_order_recovery=demo.ACTION_RECOVERY_POLICY)
    return session


def shared_answer():
    return {
        'schema_version': demo.SHARED_CAPTIONS_WIRE_VERSION,
        'language': 'ru',
        'frame_captions': {
            '1': 'Заготовка лежит на столе.',
            '2': 'Заготовка в руке над столом.',
            '3': 'Заготовка касается подставки.',
        },
        'actions': [
            {'frame_pair': '1-2', 'subject': 'Заготовка', 'title': 'Поднимаем заготовку',
             'kind': 'action', 'instruction': 'Берём заготовку и поднимаем её над столом.'},
            {'frame_pair': '2-3', 'subject': 'Заготовка', 'title': 'Опускаем на подставку',
             'kind': 'action', 'instruction': 'Опускаем заготовку на подставку.'},
        ],
        'gaps': [],
    }


def test_shared_caption_remains_identical_across_steps_after_reconciliation():
    source = frames(images(3))
    session = shared_session()
    value = shared_answer()
    content = demo.normalize_grounded_actions(value, source, session)
    projected = present_instruction(demo.reconcile_grounding(content, source, session))
    first, second = projected['steps']
    assert first['photo_demonstration'][-1] == second['photo_demonstration'][0] == {
        'frame_id': str(source[1].id), 'caption': value['frame_captions']['2'],
    }
    assert content['observations'][0]['after'] == content['observations'][1]['before']
    assert content['frame_grounding']['batches'][0]['wire'] == value


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'blank', 'long', 'action_caption'])
def test_shared_caption_contract_rejects_missing_unbound_and_competing_descriptions(mutation):
    source = frames(images(3))
    value = shared_answer()
    if mutation == 'missing':
        del value['frame_captions']['2']
    elif mutation == 'extra':
        value['frame_captions']['4'] = 'Кадр вне запроса.'
    elif mutation == 'blank':
        value['frame_captions']['2'] = ' '
    elif mutation == 'long':
        value['frame_captions']['2'] = 'а' * 161
    else:
        value['actions'][1]['before'] = 'Заготовка не видна.'
    with pytest.raises(InvalidModelOutput):
        demo.normalize_grounded_actions(value, source, shared_session())


def test_shared_caption_duplicate_key_is_rejected_in_model_json():
    with pytest.raises(InvalidModelOutput):
        parse_json('{"frame_captions":{"2":"В руке","2":"Не видна"}}')


def test_bound_shared_schema_uses_actual_frames_and_action_recovery_keeps_valid_sibling():
    source = frames(images(3))
    session = shared_session()
    value = shared_answer()
    schema = demo.bind_schema(source, wire_version=demo.SHARED_CAPTIONS_WIRE_VERSION)
    validator = Draft202012Validator(schema)
    assert validator.is_valid(value)
    value['actions'][0]['frame_pair'] = '1-4'
    assert not validator.is_valid(value)
    content = demo.normalize_grounded_actions(value, source, session)
    assert [step['title'] for step in content['steps']] == [value['actions'][1]['title']]
    assert any(warning['code'] == 'action_order_unverified' for warning in content['warnings'])
    assert demo.reconcile_grounding(content, source, session) == content
