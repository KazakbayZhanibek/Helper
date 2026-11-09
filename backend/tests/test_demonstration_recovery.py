from copy import deepcopy
import json

import pytest

from ai_drafts.validation import InvalidModelOutput
from core.services import etag
from media_pipeline.exceptions import ProcessingFailure
from media_pipeline.worker import process_one
from visual_drafts import demonstrations as demo, provider, services, worker
from visual_drafts.models import VisualProposal
from visual_drafts.teaching import present_instruction
from .conftest import post
from .test_action_protocol import images
from .test_demonstrations import answer, selected
from .test_frame_grounding import frames
from .test_visual_full_video import full_video, queue, fake_extraction


def session_with_recovery():
    session = selected()
    session.analysis_budget['action_order_recovery'] = demo.ACTION_RECOVERY_POLICY
    return session


def two_actions(kind):
    value = answer(3)
    bad = deepcopy(value['actions'][0])
    bad['title'] = 'НЕВЕРНЫЙ_СЕКРЕТНЫЙ_ШАГ'
    bad['instruction'] = 'НЕВЕРНЫЙ_СЕКРЕТНЫЙ_ТЕКСТ'
    bad['moments'][0]['visible'] = 'НЕВЕРНАЯ_СЕКРЕТНАЯ_ПОДПИСЬ'
    if kind == 'reversed': bad['moments'].reverse()
    if kind == 'duplicate': bad['moments'][-1]['image_index'] = 1
    if kind == 'out_of_batch': bad['moments'][-1]['image_index'] = 4
    if kind == 'above_schema': bad['moments'][-1]['image_index'] = 13
    if kind == 'below_schema': bad['moments'][0]['image_index'] = 0
    value['actions'].insert(0, bad)
    return value


@pytest.mark.parametrize('kind', ['reversed', 'duplicate', 'out_of_batch', 'above_schema', 'below_schema'])
def test_recovery_drops_only_invalid_order_without_sorting_or_exposing_it(kind):
    source = frames(images(3))
    session = session_with_recovery()
    wire = two_actions(kind)
    original = deepcopy(wire)
    content = demo.normalize_grounded_actions(wire, source, session)
    assert wire == original and content['frame_grounding']['batches'][0]['wire'] == original
    assert len(content['steps']) == 1 and content['steps'][0]['title'] == original['actions'][1]['title']
    assert content['observations'][0]['id'].endswith(':o2')
    assert any(g['evidence_frame_ids'] == [str(f.id) for f in source] for g in content['gaps'])
    assert any(w['code'] == 'action_order_unverified' for w in content['warnings'])
    visible = {key: value for key, value in content.items() if key != 'frame_grounding'}
    assert 'СЕКРЕТН' not in json.dumps(visible, ensure_ascii=False)
    shown = present_instruction(demo.reconcile_grounding(content, source, session))
    assert len(shown['steps'][0]['photo_demonstration']) == 2
    assert all('СЕКРЕТН' not in item['caption'] for item in shown['steps'][0]['photo_demonstration'])


def test_legacy_sessions_still_fail_closed_on_invalid_order():
    source = frames(images(3))
    with pytest.raises(InvalidModelOutput, match='VISUAL_EVIDENCE_ORDER_INVALID'):
        demo.normalize_grounded_actions(two_actions('reversed'), source, selected())


def test_all_rejected_actions_remain_a_review_question_not_a_fake_step():
    source = frames(images(3))
    value = two_actions('reversed')
    value['actions'].pop()
    content = demo.normalize_grounded_actions(value, source, session_with_recovery())
    assert content['steps'] == [] and content['observations'] == []
    assert any(w['code'] == 'action_order_unverified' for w in content['warnings'])
    assert any(w['code'] == 'insufficient_evidence' for w in content['warnings'])
    assert demo.reconcile_grounding(content, source, session_with_recovery()) == content


def test_span_session_rejects_checkpoint_from_old_wire():
    session = session_with_recovery()
    session.analysis_budget['demonstration_wire'] = demo.SPAN_WIRE_VERSION
    checkpoint = {'frame_grounding': {'version': 'v3', 'batches': [{'wire': answer(3)}]}}
    with pytest.raises(ProcessingFailure) as error:
        worker.check_grounding_checkpoint(checkpoint, session, window=True)
    assert error.value.code == 'VISUAL_PROTOCOL_INVALID'


@pytest.mark.parametrize('gap', [
    {'from_image_index': 3, 'to_image_index': 2, 'reason': 'cut', 'question': 'Переход?'},
    {'from_image_index': 1, 'to_image_index': 13, 'reason': 'cut', 'question': 'Переход?'},
    {'from_image_index': 1, 'to_image_index': 4, 'reason': 'unclear_action', 'question': 'Переход?'},
])
def test_invalid_gap_never_allows_a_partial_result(gap):
    source = frames(images(3))
    value = two_actions('duplicate')
    value['gaps'] = [gap]
    with pytest.raises(InvalidModelOutput):
        demo.normalize_grounded_actions(value, source, session_with_recovery())


@pytest.mark.django_db
def test_worker_stores_wire_but_api_and_apply_exclude_invalid_action(team, full_video, settings, monkeypatch):
    settings.VISUAL_ACTION_EVIDENCE = settings.VISUAL_SHORT_ACTION_COVERAGE = settings.VISUAL_FRAME_GROUNDING = True
    settings.VISUAL_FRAME_GROUNDING_VERSION = 'v3'
    class Fake(provider.VisualProvider):
        provider, model = 'openai', 'gpt-5.4-mini'
        endpoint = 'https://api.openai.com/v1'
        def __init__(self, organization): pass
        def generate(self, *, frames, **kwargs):
            result = answer(len(frames))
            bad = deepcopy(result['actions'][0])
            bad['title'] = 'НЕВЕРНЫЙ_СЕКРЕТНЫЙ_ШАГ'
            bad['instruction'] = 'НЕВЕРНЫЙ_СЕКРЕТНЫЙ_ТЕКСТ'
            bad['moments'].reverse()
            result['actions'].insert(0, bad)
            return result
    monkeypatch.setattr(worker, 'VisualProvider', Fake)
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
    job, live = queue(team, full_video, max_ai_calls=3)
    assert live.analysis_budget['action_order_recovery'] == job.payload['action_order_recovery'] == demo.ACTION_RECOVERY_POLICY
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done'
    proposal = VisualProposal.objects.get()
    assert 'СЕКРЕТН' in json.dumps(proposal.content, ensure_ascii=False)
    shown = services.proposal_data(proposal)
    assert shown['can_apply'] and len(shown['steps']) == 3
    assert 'СЕКРЕТН' not in json.dumps(shown, ensure_ascii=False)
    assert any(w['code'] == 'action_order_unverified' for w in shown['warnings'])
    response = post(team.clients['author'], full_video.url + f'/visual-proposals/{proposal.id}/apply',
                    {'confirm_replace': True}, etag=etag(full_video.working), key='recovered-apply')
    assert response.status_code == 200, response.json()
    assert 'СЕКРЕТН' not in json.dumps(response.json(), ensure_ascii=False)
    assert len(response.json()['steps']) == 3


@pytest.mark.django_db
@pytest.mark.parametrize('shared_captions', [False, True])
def test_span_wire_is_pinned_and_worker_applies_the_same_two_photos(team, full_video, settings, monkeypatch, shared_captions):
    settings.VISUAL_ACTION_EVIDENCE = settings.VISUAL_SHORT_ACTION_COVERAGE = settings.VISUAL_FRAME_GROUNDING = True
    settings.VISUAL_FRAME_GROUNDING_VERSION = 'v3'
    settings.VISUAL_DEMONSTRATION_SPANS = True
    settings.VISUAL_DEMONSTRATION_SHARED_CAPTIONS = shared_captions
    wire_version = demo.SHARED_CAPTIONS_WIRE_VERSION if shared_captions else demo.SPAN_WIRE_VERSION

    class Fake(provider.VisualProvider):
        provider, model = 'openai', 'gpt-5.4-mini'
        endpoint = 'https://api.openai.com/v1'

        def __init__(self, organization):
            pass

        def generate(self, *, frames, **kwargs):
            assert self.demonstration_wire == wire_version
            action = {
                'subject': 'Крышка', 'title': 'Поднимаем крышку', 'frame_pair': '1-2', 'kind': 'action',
                'instruction': 'Берём крышку за край и поднимаем её.',
            }
            before, after = 'Крышка лежит на коробке.', 'Крышка в руке над коробкой.'
            response = {'schema_version': wire_version, 'language': 'ru', 'gaps': [], 'actions': [action]}
            if shared_captions:
                response['frame_captions'] = {'1': before, '2': after}
            else:
                action.update(before=before, after=after, change='Крышка над коробкой.')
            return response

    monkeypatch.setattr(worker, 'VisualProvider', Fake)
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
    job, session = queue(team, full_video, max_ai_calls=3)
    assert session.analysis_budget['demonstration_wire'] == job.payload['demonstration_wire'] == wire_version
    # Workers must use the queued policy even if the setting changes before execution.
    settings.VISUAL_DEMONSTRATION_SPANS = False
    settings.VISUAL_DEMONSTRATION_SHARED_CAPTIONS = False
    assert process_one()
    job.refresh_from_db()
    assert job.state == 'done'
    proposal = VisualProposal.objects.get()
    shown = services.proposal_data(proposal)
    assert shown['can_apply'] and len(shown['steps']) == 3
    photos = [step['photo_demonstration'] for step in shown['steps']]
    for step, pair in zip(shown['steps'], photos):
        assert [photo['frame_id'] for photo in pair] == step['evidence_frame_ids']
        assert len(set(step['evidence_frame_ids'])) == 2
    response = post(team.clients['author'], full_video.url + f'/visual-proposals/{proposal.id}/apply',
                    {'confirm_replace': True}, etag=etag(full_video.working), key='pair-apply')
    assert response.status_code == 200, response.json()
    assert [step['photo_demonstration'] for step in response.json()['steps']] == photos
