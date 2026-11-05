"""Learner prose projection: no inference, no rewriting of saved model facts."""
from copy import deepcopy
import uuid

import pytest

from visual_drafts import services
from visual_drafts.models import VisualProposal
from visual_drafts.teaching import present_instruction, PROCESS_STYLE
from visual_drafts.provider import request_contract
from .test_action_protocol import images
from .test_action_evidence_integration import structured_proposal, apply


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('A presentation change must not call a model')
    from visual_drafts import provider
    from ai_drafts import codex_provider
    monkeypatch.setattr(provider.VisualProvider, 'generate', forbidden)
    monkeypatch.setattr(provider, 'json_request', forbidden)
    monkeypatch.setattr(codex_provider, 'generate', forbidden)


@pytest.mark.django_db
@pytest.mark.parametrize('subject,method', [
    ('Тесто', 'Теперь замешиваем тесто руками. Складываем край к середине и прижимаем ладонью, затем повторяем движение.'),
    ('Ткань', 'Прокладываем строчку вдоль края ткани. Направляем ткань обеими руками под лапку.'),
    ('Деталь', 'Переходим к обработке края детали. Удерживаем её левой рукой и проводим инструментом вдоль края.'),
])
def test_same_plain_explanation_is_previewed_and_saved_across_processes(team, draft, subject, method):
    from visual_drafts.teaching import describe_action
    selected, proposal, frames = structured_proposal(team, draft)
    content = deepcopy(proposal.content)
    observation = content['observations'][0]
    observation.update(subject=subject, movement=method)
    content['steps'][0]['description'] = describe_action(observation, language='ru', step_format='teaching_v1')
    proposal = VisualProposal.objects.create(organization=proposal.organization, session=selected,
        source_job_id=uuid.uuid4(), provider="test", model="fixture", content=content)
    original = deepcopy(proposal.content)
    preview = services.proposal_data(proposal)
    assert preview == services.proposal_data(proposal)
    assert preview['steps'][0]['description'] == method
    assert preview['observations'] == original['observations']
    assert preview['steps'][0]['evidence_frame_ids'] == original['steps'][0]['evidence_frame_ids']
    assert 'С чем работаем' not in preview['steps'][0]['description']
    response = apply(team, draft, proposal)
    assert response.status_code == 200, response.json()
    assert response.json()['steps'][0]['description'] == method
    assert [photo['id'] for photo in response.json()['steps'][0]['evidence_frames']] == preview['steps'][0]['evidence_frame_ids']
    proposal.refresh_from_db()
    assert proposal.content == original
    # Applied history remains the archived source, not a retroactive rewrite.
    assert services.proposal_data(proposal)['steps'] == original['steps']


@pytest.mark.django_db
def test_presentation_cannot_hide_a_tampered_original_step(team, draft):
    _, proposal, _ = structured_proposal(team, draft)
    proposal.content['steps'][0]['description'] = 'Теперь добавляем невидимый ингредиент.'
    proposal = VisualProposal.objects.create(organization=proposal.organization, session=proposal.session,
        source_job_id=uuid.uuid4(), provider="test", model="fixture", content=proposal.content)
    preview = services.proposal_data(proposal)
    assert not preview['can_apply']
    assert 'presentation_format' not in preview
    with pytest.raises(ValueError):
        present_instruction(proposal.content)
    response = apply(team, draft, proposal)
    assert response.status_code == 422


@pytest.mark.django_db
def test_projection_is_pure_and_does_not_invent_connections_or_results(team, draft):
    _, proposal, _ = structured_proposal(team, draft)
    original = deepcopy(proposal.content)
    shown = present_instruction(proposal.content)
    assert proposal.content == original
    assert shown['steps'][0]['description'] == original['observations'][0]['movement']
    assert shown['gaps'] == original['gaps'] and shown['warnings'] == original['warnings']


@pytest.mark.parametrize('protocol,coverage,grounding', [
    ('2', None, None), ('3', None, None), ('3', 'v1', None),
    ('3', 'v1', 'v1'), ('3', 'v1', 'v2'),
])
def test_all_upload_modes_use_the_same_process_style_within_existing_input_limit(protocol, coverage, grounding):
    import json
    system, schema = request_contract(protocol, 'ru', 'upload', images(12),
        short_action_coverage=coverage, frame_grounding=grounding)
    assert system.count(PROCESS_STYLE) == 1
    assert len(json.dumps({'system': system, 'schema': schema}, ensure_ascii=False).encode()) + 2048 < 32768


@pytest.mark.parametrize('protocol', ['2', '3'])
def test_live_observations_do_not_become_a_completed_story(protocol):
    system, _ = request_contract(protocol, 'ru', 'live', images(2))
    assert PROCESS_STYLE not in system
