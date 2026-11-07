from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from ai_drafts.validation import InvalidModelOutput
from visual_drafts import demonstrations as demo
from visual_drafts.teaching import present_instruction
from .test_action_protocol import images
from .test_demonstrations import answer, selected
from .test_frame_grounding import frames


def span_session():
    session = selected()
    session.analysis_budget.update(demonstration_wire=demo.SPAN_WIRE_VERSION,
                                   action_order_recovery=demo.ACTION_RECOVERY_POLICY)
    return session


def span_answer(pair='1-3'):
    result = answer()
    result['schema_version'] = demo.SPAN_WIRE_VERSION
    action = result['actions'][0]
    before, after = action.pop('moments')
    action.update(frame_pair=pair, before=before['visible'], after=after['visible'])
    return result


@pytest.mark.parametrize('pair', ['1-1', '3-1', '1-4'])
def test_bound_schema_excludes_duplicate_reversed_and_absent_frames(pair):
    source = frames(images(3))
    validator = Draft202012Validator(demo.bind_schema(source, wire_version=demo.SPAN_WIRE_VERSION))
    assert validator.is_valid(span_answer('1-3'))
    assert not validator.is_valid(span_answer(pair))


def test_exact_endpoint_captions_survive_reconciliation_and_legacy_wire_stays_valid():
    source = frames(images(3))
    value = span_answer()
    session = span_session()
    content = demo.normalize_grounded_actions(value, source, session)
    step = present_instruction(demo.reconcile_grounding(content, source, session))['steps'][0]
    assert step['evidence_frame_ids'] == [str(frame.id) for frame in source]
    assert step['photo_demonstration'] == [
        {'frame_id': str(source[0].id), 'caption': value['actions'][0]['before']},
        {'frame_id': str(source[-1].id), 'caption': value['actions'][0]['after']},
    ]
    legacy = demo.normalize_grounded_actions(answer(), source, selected())
    assert present_instruction(demo.reconcile_grounding(legacy, source, selected()))['steps'][0]['photo_demonstration'] == step['photo_demonstration']
    with pytest.raises(InvalidModelOutput):
        demo.reconcile_grounding(content, source, selected())


def test_invalid_pair_drops_one_action_but_not_a_valid_sibling():
    source = frames(images(3))
    session = span_session()
    value = span_answer()
    invalid = deepcopy(value['actions'][0])
    invalid.update(frame_pair='2-2', title='Движение без второй фотографии')
    value['actions'].insert(0, invalid)
    content = demo.normalize_grounded_actions(value, source, session)
    assert [step['title'] for step in content['steps']] == [value['actions'][1]['title']]
    assert any(warning['code'] == 'action_order_unverified' for warning in content['warnings'])
    assert demo.reconcile_grounding(content, source, session) == content
