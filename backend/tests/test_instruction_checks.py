"""No inference calls: conservative command and gap projection regressions."""
from copy import deepcopy
import uuid

import pytest

from ai_drafts.validation import InvalidModelOutput
from ai_budget.models import AIUsageReservation
from visual_drafts import short_actions, services
from visual_drafts.instruction_checks import uncertain_command, UNCERTAIN_COMMAND_REASON
from visual_drafts.teaching import describe_action
from .test_short_action_coverage import images, wire, normalize, trusted_frames, session
from .test_action_link_projection import legacy, distinct_candidate
from .conftest import post


@pytest.mark.parametrize('text', [
    'Подведите лезвие справа налево?', 'Возможно, поднимите деталь.',
    'Предположительно, поверните рычаг.', 'Perhaps turn the handle.',
    'Бәлкім, тұтқаны бұраңыз.', 'Поднимите деталь？',
])
def test_explicitly_uncertain_command_is_review_question_not_instruction(text):
    source = images(2)
    value = wire(source)
    value['observations'][0]['movement'] = text
    original = deepcopy(value)
    result = normalize(value, source)
    assert result['steps'] == result['observations'] == []
    assert any(g['description'] == UNCERTAIN_COMMAND_REASON for g in result['gaps'])
    assert all(text not in g['description'] for g in result['gaps'])
    assert value == original
    assert short_actions.reconcile_action_links(result, trusted_frames(source), session()) == result


@pytest.mark.parametrize('text', [
    'Прижмите крышку ладонью и поверните её по часовой стрелке.',
    'Проведите щёткой по поверхности; повторите движение в ту же сторону.',
    'Сложите тесто и прижмите его ладонью.',
    'Держите деталь, пока она не окажется напротив отверстия.',
])
def test_confident_commands_in_different_processes_remain_unchanged(text):
    source = images(2)
    value = wire(source)
    value['observations'][0]['movement'] = text
    result = normalize(value, source)
    assert len(result['steps']) == 1
    assert result['observations'][0]['movement'] == text


def test_question_in_state_or_missing_information_is_not_an_imperative():
    assert not uncertain_command({'kind': 'state', 'title': 'Что находится на поверхности?', 'movement': None})
    assert not uncertain_command({'kind': 'action', 'title': 'Поверните деталь', 'movement': 'Поверните деталь.',
                                  'after': 'Возможно, отверстие скрыто от камеры.'})


@pytest.mark.parametrize('reason', ['sampling_gap', 'occluded', 'cut', 'outside_recording', 'unclear_action'])
def test_gap_does_not_repeat_unverified_model_premise(reason):
    source = images(2)
    value = wire(source)
    value['observations'] = []
    value['transitions'][0].update(change='unexplained_change', observation_ids=[])
    text = 'Предмет положили в ёмкость, которой нет на выбранных изображениях.'
    value['gaps'] = [{'reason': reason, 'description': text, 'evidence_frame_ids': [f['id'] for f in source]}]
    result = normalize(value, source)
    assert len(result['gaps']) == 1
    assert result['gaps'][0]['description'] != text
    assert result['gaps'][0]['reason'] == reason
    assert result['gaps'][0]['evidence_frame_ids'] == [f['id'] for f in source]
    assert value['gaps'][0]['description'] == text


@pytest.mark.parametrize('corruption', ['foreign', 'cut'])
def test_uncertainty_does_not_hide_invalid_evidence(corruption):
    source = images(2)
    value = wire(source)
    value['observations'][0]['movement'] = 'Поднимите деталь?'
    if corruption == 'foreign':
        value['observations'][0]['after_frame_id'] = str(uuid.uuid4())
    else:
        value['transitions'][0].update(change='cut', observation_ids=[])
    with pytest.raises(InvalidModelOutput):
        normalize(value, source)


@pytest.mark.django_db
def test_stored_question_command_is_filtered_identically_on_read_and_apply(team, legacy):
    content = deepcopy(legacy.content)
    first = content['observations'][0]
    first['movement'] = 'Подведите деталь справа налево?'
    content['steps'][0]['description'] = describe_action(first, language=content['language'], step_format=content['step_format'])
    distinct_candidate(legacy, content)
    shown = team.clients['author'].get(legacy.url).json()
    assert not shown['can_apply'] and shown['steps'] == []
    assert any(g['description'] == UNCERTAIN_COMMAND_REASON for g in shown['gaps'])
    applied = post(team.clients['author'], legacy.url + '/apply', {'confirm_replace': True},
                   etag=legacy.draft.working['etag'], key='uncertain-command')
    assert applied.status_code == 422 and applied.json()['error']['code'] == 'NO_PROPOSED_STEPS'
    legacy.item.refresh_from_db()
    assert legacy.item.content == content and legacy.item.applied_at is None
    assert AIUsageReservation.objects.count() == 0


def test_grounding_pin_cannot_be_dropped_or_added_at_projection():
    source = images(2)
    value = normalize(wire(source), source)
    selected = session()
    selected.analysis_budget = {'visual_protocol': '3', 'short_action_coverage': 'v1', 'frame_grounding': 'v1'}
    with pytest.raises(InvalidModelOutput, match='VISUAL_GROUNDING_PROTOCOL_INVALID'):
        services.checked_instruction_content(value, trusted_frames(source), selected)
    selected.analysis_budget = {}
    value['frame_grounding'] = {'version': 'v1', 'batches': []}
    with pytest.raises(InvalidModelOutput, match='VISUAL_GROUNDING_PROTOCOL_INVALID'):
        services.checked_instruction_content(value, trusted_frames(source), selected)
