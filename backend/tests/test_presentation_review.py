from copy import deepcopy

import pytest

from ai_budget.models import AIUsageReservation
from core.services import etag
from visual_drafts.presentation_review import review_step
from visual_drafts.teaching import present_instruction
from visual_drafts import provider, services, worker
from visual_drafts.models import VisualProposal
from media_pipeline.worker import process_one
from .conftest import post
from .test_demonstrations import normalized, answer
from .test_visual_full_video import full_video, queue, fake_extraction


def test_terminal_purpose_removed_without_removing_the_visible_action():
    text = 'Складываем материал и прижимаем его, чтобы выработать его структуру и сделать однородным.'
    projected, photos, warning = review_step(text, [], language='ru')
    assert projected == 'Складываем материал и прижимаем его.'
    assert photos == [] and warning


def test_ambiguous_purpose_with_another_action_is_preserved_and_flagged():
    text = 'Поднимаем предмет, чтобы проверить его и равномерно покрыть поверхность.'
    projected, _, warning = review_step(text, [], language='ru')
    assert projected == text and warning


def test_mixed_language_fragment_hides_caption_not_photo_or_source():
    photos = [{'frame_id': 'frame-1', 'caption': 'Рука нажимает, worked out its structure.'},
              {'frame_id': 'frame-2', 'caption': 'Проверяем USB Type C на панели.'}]
    original = deepcopy(photos)
    _, projected, warning = review_step('Нажимаем на поверхность.', photos, language='ru')
    assert warning and projected[0] == {'frame_id': 'frame-1', 'caption': ''}
    assert projected[1] == original[1] and photos == original


def test_mixed_word_is_suppressed_without_losing_photo_and_brand_words_survive():
    photos = [{'frame_id': '1', 'caption': 'Тесто в kompактную форму.'},
              {'frame_id': '2', 'caption': 'Кабель USB-C подключён к панели.'},
              {'frame_id': '3', 'caption': 'Кнопка WiFi на панели.'}]
    original = deepcopy(photos)
    _, updated, warning = review_step('Работаем с предметом.', photos, language='ru')
    assert warning and updated[0] == {'frame_id': '1', 'caption': ''}
    assert updated[1:] == original[1:] and photos == original


def test_photo_number_prefix_removed_only_if_it_is_unambiguously_a_label():
    photos = [{'frame_id': '1', 'caption': 'На фото 3 кусок лежит на столе.'},
              {'frame_id': '2', 'caption': 'Фото №12: рука удерживает деталь.'},
              {'frame_id': '3', 'caption': 'Сверьте расположение с фото 3.'}]
    _, updated, warning = review_step('Удерживаем деталь.', photos, language='ru')
    assert warning
    assert [p['caption'] for p in updated] == ['Кусок лежит на столе.', 'Рука удерживает деталь.', 'Сверьте расположение с фото 3.']
    assert [p['frame_id'] for p in updated] == ['1', '2', '3']


@pytest.mark.parametrize('title', ['Формовка материала', 'Формирование детали', 'Финальная отделка', 'Завершение сборки'])
def test_outcome_claim_in_title_requires_review_without_rewriting_title(title):
    description = 'Нажимаем на предмет.'
    text, _, warning = review_step(description, [], language='ru', title=title)
    assert warning and text == description


def test_legacy_presentations_are_unchanged_and_new_projection_is_explicit():
    value = answer()
    value['actions'][0]['instruction'] = 'Нажимаем на деталь, чтобы улучшить её структуру.'
    value['actions'][0]['moments'][0]['visible'] = 'Рука касается детали, worked out its structure.'
    content, _ = normalized(value)
    original = deepcopy(content)
    legacy = present_instruction(content)
    updated = present_instruction(content, presentation_policy='cautious_v1')
    assert legacy['steps'][0]['description'] == value['actions'][0]['instruction']
    assert updated['steps'][0]['description'] == 'Нажимаем на деталь.'
    assert updated['steps'][0]['photo_demonstration'][0]['caption'] == ''
    assert updated['steps'][0]['photo_demonstration'][0]['frame_id'] == legacy['steps'][0]['photo_demonstration'][0]['frame_id']
    assert updated['steps'][0]['presentation_review'] is True
    assert updated['steps'][0]['start_ms'] == legacy['steps'][0]['start_ms']
    assert content == original


@pytest.mark.django_db
def test_new_session_read_and_apply_use_same_projection_and_preserve_frame_ids(team, full_video, settings, monkeypatch):
    settings.VISUAL_ACTION_EVIDENCE = settings.VISUAL_SHORT_ACTION_COVERAGE = settings.VISUAL_FRAME_GROUNDING = True
    settings.VISUAL_FRAME_GROUNDING_VERSION = 'v3'
    class Fake(provider.VisualProvider):
        provider, model = 'openai', 'gpt-5.4-mini'
        endpoint = 'https://api.openai.com/v1'
        def __init__(self, organization): pass
        def generate(self, *, frames, **kwargs):
            result = answer(len(frames))
            result['actions'][0]['instruction'] = 'Нажимаем на деталь, чтобы улучшить её структуру.'
            result['actions'][0]['moments'][0]['visible'] = 'Рука на детали, worked out its structure.'
            return result
    monkeypatch.setattr(worker, 'VisualProvider', Fake)
    monkeypatch.setattr(worker, 'uploaded_frames', fake_extraction)
    job, live = queue(team, full_video, max_ai_calls=3)
    assert live.analysis_budget['presentation_policy'] == 'cautious_v1'
    assert process_one()
    item = VisualProposal.objects.get()
    raw = deepcopy(item.content)
    shown = services.proposal_data(item)
    assert shown['steps'][0]['description'] == 'Нажимаем на деталь.'
    assert shown['steps'][0]['presentation_review'] is True
    assert shown['steps'][0]['photo_demonstration'][0]['caption'] == ''
    assert len(shown['steps'][0]['evidence_frame_ids']) >= 2
    assert AIUsageReservation.objects.count() == 3
    applied = post(team.clients['author'], full_video.url+f'/visual-proposals/{item.id}/apply',
                   {'confirm_replace': True}, etag=etag(full_video.working), key='presentation-apply')
    assert applied.status_code == 200, applied.json()
    saved = applied.json()['steps'][0]
    assert saved['description'] == shown['steps'][0]['description']
    assert saved['photo_demonstration'] == shown['steps'][0]['photo_demonstration']
    assert saved['evidence_frame_ids'] == shown['steps'][0]['evidence_frame_ids']
    assert 'presentation_review' in saved['warning_codes']
    item.refresh_from_db()
    assert item.content == raw
