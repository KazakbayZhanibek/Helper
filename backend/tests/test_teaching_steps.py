"""Instruction projection checks; these local fixtures do not test recognition."""
from copy import deepcopy
import uuid

import pytest

from visual_drafts.action_evidence import normalize
from visual_drafts import services
from visual_drafts.models import VisualProposal
from visual_drafts.teaching import STEP_FORMAT, describe_action
from .test_action_evidence import frames, session, payload, initial_state
from .test_action_evidence_integration import apply, content_v3, structured_proposal
from .test_visual_drafts import add_frames, create_session


@pytest.mark.parametrize('subject,movement,after', [
    ('Ткань', 'Направляйте ткань обеими руками под лапку.', 'Край ткани проходит справа от лапки.'),
    ('Деревянная деталь', 'Удерживайте деталь левой рукой и подведите инструмент правой.', 'Инструмент касается верхней поверхности детали.'),
    ('Тесто', 'Прижмите тесто ладонью и заверните край к середине.', 'Загнутый край лежит поверх середины.'),
])
def test_different_processes_get_the_same_teaching_structure_without_new_facts(frames, session, subject, movement, after):
    value = payload(frames)
    observation = value['observations'][0]
    observation.update(subject=subject, movement=movement, after=after)
    snapshot = deepcopy(value)
    content = normalize(value, frames, session)
    description = content['steps'][0]['description']
    assert description.split('\n\n') == [
        f'С чем работаем\n{subject}',
        f"Перед началом на видео\n{observation['before']}",
        f'Как выполнить\n{movement}',
        f'Что сравнить с видео после действия\n{after}',
    ]
    assert value == snapshot
    assert content['steps'][0]['title'] == observation['title']


def test_unseen_preparation_is_omitted_and_unseen_result_is_explicit(frames, session):
    value = payload(frames)
    observed = value['observations'][0]
    observed.update(before=None, before_frame_id=None, after=None, after_frame_id=None,
                    evidence_frame_ids=observed['action_frame_ids'])
    content = normalize(value, frames, session)
    description = content['steps'][0]['description']
    assert 'Перед началом' not in description
    assert observed['movement'] in description
    assert 'Результат на этих кадрах не показан.' in description
    assert 'Уточните у мастера' in description
    assert content['observations'][0]['after'] is None


@pytest.mark.parametrize('language,labels', [
    ('ru', ('С чем работаем', 'Как выполнить')),
    ('kk', ('Немен жұмыс істейміз', 'Қалай орындау керек')),
    ('mixed', ('С чем работаем', 'Как выполнить')),
])
def test_teaching_labels_follow_language_without_translating_observations(frames, session, language, labels):
    session.language = language
    value = payload(frames)
    value['language'] = language
    value['observations'][0]['movement'] = 'Бөлшекті оңға жылжытыңыз. Move the part right.'
    content = normalize(value, frames, session)
    assert all(label in content['steps'][0]['description'] for label in labels)
    assert value['observations'][0]['movement'] in content['steps'][0]['description']


def test_live_remains_factual_without_teaching_metadata(frames, session):
    session.mode = 'live'
    value = payload(frames)
    content = normalize(value, frames, session)
    assert 'step_format' not in content
    assert content['steps'][0]['description'] == value['observations'][0]['movement']
    assert 'preliminary_live' in {warning['code'] for warning in content['warnings']}


def test_state_never_becomes_a_teaching_step(frames, session):
    value = payload(frames)
    value['observations'] = [initial_state(frames)]
    content = normalize(value, frames, session)
    assert content['step_format'] == STEP_FORMAT
    assert content['steps'] == []
    with pytest.raises(ValueError):
        describe_action(value['observations'][0], language='ru', step_format=STEP_FORMAT)


@pytest.mark.parametrize('language', ['ru', 'kk', 'mixed'])
def test_maximum_schema_fields_fit_the_working_step_limit(language):
    observation = {'kind': 'action', 'subject': 'П' * 120, 'before': 'Д' * 240,
                   'movement': 'М' * 800, 'after': 'Р' * 240}
    description = describe_action(observation, language=language, step_format=STEP_FORMAT)
    assert len(description) <= 2000
    assert all(value in description for key, value in observation.items() if key != 'kind')


@pytest.mark.parametrize('language', ['ru', 'kk', 'mixed'])
def test_maximum_fields_with_missing_result_stay_bounded(language):
    observation = {'kind': 'action', 'subject': 'П' * 120, 'before': 'Д' * 240,
                   'movement': 'М' * 800, 'after': None}
    assert len(describe_action(observation, language=language, step_format=STEP_FORMAT)) <= 2000


def test_future_oversized_projection_is_rejected_without_truncation():
    observation = {'kind': 'action', 'subject': 'Предмет', 'before': None,
                   'movement': 'М' * 2001, 'after': None}
    with pytest.raises(ValueError, match='limit'):
        describe_action(observation, language='ru', step_format=STEP_FORMAT)


@pytest.mark.django_db
def test_readable_explanation_is_applied_while_archived_projection_is_preserved(team, draft):
    session, proposal, frames = structured_proposal(team, draft)
    description = proposal.content['steps'][0]['description']
    assert proposal.content['step_format'] == STEP_FORMAT
    assert 'Как выполнить\n' in description and 'Что сравнить с видео после действия\n' in description
    preview = services.proposal_data(proposal)
    readable = proposal.content['observations'][0]['movement']
    assert preview['presentation_format'] == 'process_story_v1'
    assert preview['steps'][0]['description'] == readable
    response = apply(team, draft, proposal)
    assert response.status_code == 200, response.json()
    assert session.instruction.working_copy.steps.get().description == readable
    assert response.json()['steps'][0]['description'] == readable
    proposal.refresh_from_db()
    assert proposal.content['step_format'] == STEP_FORMAT
    assert proposal.content['steps'][0]['description'] == description


@pytest.mark.django_db
def test_historical_v3_movement_remains_readable_and_applicable(team, draft):
    session = create_session(team, draft, mode='upload', state='completed')
    frames = add_frames(session)
    content = content_v3(frames)
    assert 'step_format' not in content
    proposal = VisualProposal.objects.create(organization=team.org, session=session, source_job_id=uuid.uuid4(), provider='test', model='fixture', content=content)
    assert services.proposal_data(proposal)['can_apply']
    response = apply(team, draft, proposal)
    assert response.status_code == 200, response.json()
    assert session.instruction.working_copy.steps.get().description == content['observations'][0]['movement']


@pytest.mark.django_db
@pytest.mark.parametrize('mutation', ['unknown_format', 'missing_format', 'subject', 'before', 'after', 'movement'])
def test_teaching_projection_or_format_cannot_change_without_revalidation(team, draft, mutation):
    _, proposal, frames = structured_proposal(team, draft)
    content = deepcopy(proposal.content)
    if mutation == 'unknown_format':
        content['step_format'] = 'unknown'
    elif mutation == 'missing_format':
        content.pop('step_format')
    else:
        content['observations'][0][mutation] = 'Изменённое утверждение.'
    assert not services.action_links_valid(content, {str(frame.id): frame for frame in frames})
