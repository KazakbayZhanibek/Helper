"""Local wording controls, not an assessment of recognition accuracy."""
from copy import deepcopy

import pytest

from visual_drafts.prose_evidence import REVIEW_REASON, unsupported_detail


@pytest.mark.parametrize('text', [
    'Нажимайте на крышку до щелчка.',
    'Дождитесь характерного щелчка и отпустите рычаг.',
    'Вращайте винт, пока не почувствуете сопротивление.',
    'Не ослабляйте зажим, пока не услышите щелчок.',
    'Продолжайте, пока вы не услышите звук.',
    'Прислушайтесь к работе механизма.',
    'Ощутите вибрацию детали.',
    'Проверьте готовность по запаху.',
    'Определите нагрев на ощупь.',
    'Закройте крышку: слышен щелчок.',
    'Wait for a click before releasing the lever.',
    'Turn until you feel resistance.',
    'Do not tighten until you hear a click; observe the position.',
    'Check readiness by smell.',
    'Шерту дыбысын естігенше басыңыз.',
    'Қарсылықты сезгенше бұраңыз.',
    'Иісіне қарап тексеріңіз.',
])
def test_unavailable_sensory_criterion_is_flagged(text):
    assert unsupported_detail({'title': 'Выполните действие', 'movement': text}) == REVIEW_REASON


@pytest.mark.parametrize('text', [
    'Прижмите деталь с усилием 20 Н.',
    'Приложите усилие 20 Н.',
    'Примените силу 15 N.',
    'Затяните винт с моментом затяжки 5 Н·м.',
    'Поддерживайте давление 3 бар.',
    'Выставьте температуру на 80 °C.',
    'Нагрейте деталь до 80 °C.',
    'Охладите ёмкость до −5 °С.',
    'Затяните крепёж до 3,5 Нм.',
    'Затяните крепёж до 3,5 Н м.',
    'Tighten the bolt to 5 N m.',
    'Вращайте вал со скоростью 200 об/мин.',
    'Выставьте обороты 900 rpm.',
    'Установите давление не более 2–3 кПа.',
    'Set the temperature to 80 °C.',
    'Tighten the screw to 5 N*m.',
    'Maintain pressure at 10 psi.',
    'Қысымды 3 bar етіп орнатыңыз.',
    'Выставьте температуру на ８０ ℃.',
])
def test_setting_requires_operating_context_number_and_supported_unit(text):
    assert unsupported_detail({'title': '', 'movement': text}) == REVIEW_REASON


@pytest.mark.parametrize('text', [
    'Этот способ гарантирует полную безопасность.',
    'Крепление гарантированно безопасно.',
    'Такое движение полностью безопасно.',
    'Это исключает любой риск травмы.',
    'Этот порядок гарантирует качество.',
    'This method guarantees safety.',
    'The motion is completely safe.',
    'Use this risk-free method.',
    'Бұл тәсіл толық қауіпсіз.',
    'Бұл сапасына кепілдік береді.',
])
def test_categorical_guarantee_is_not_derived_from_images(text):
    assert unsupported_detail({'movement': text}) == REVIEW_REASON


@pytest.mark.parametrize('text', [
    'Нажмите на крышку ладонью и отпустите её.',
    'Закройте крышку.',
    'Вращайте винт по часовой стрелке.',
    'Поднесите ёмкость к носу.',
    'Потрогайте деталь пальцами.',
    'Сдвиньте деталь, не отрывая от поверхности.',
    'Держите деталь, пока она не окажется напротив отверстия.',
    'Перенесите блок 12 В на полку.',
    'Установите блок 12 В рядом с корпусом.',
    'Выставьте отвёртку под углом 45 градусов.',
    'Поверните деталь на 90°.',
    'Повторите движение 3 раза.',
    'Сдвиньте деталь на 20 мм.',
    'Уберите ключ 5 Нм в коробку.',
    'Покажите надпись 80 °C на корпусе.',
    'Установите деталь: на маркировке написано 80 °C.',
    'Выставьте температуру на видимую отметку.',
    'Поддерживайте давление.',
    'На поверхности появились капли.',
    'Не прислушайтесь к механизму, а посмотрите на рычаг.',
    'Не ориентируйтесь на щелчок.',
    'Не затягивайте до щелчка: ориентируйтесь на положение.',
    'Не дожимайте крышку до щелчка.',
    'Этот способ не гарантирует безопасность.',
    'Движение не полностью безопасно.',
    'Не считайте действие полностью безопасным.',
    'Never call this completely safe.',
    'Do not wait for a click.',
    'This method is not completely safe.',
    'Жайлап қақпақты жабыңыз.',
    'Бөлшекті қолмен жылжытыңыз.',
    'Прочитайте надпись «при температуре 80 °C».',
    'Покажите маркировку "с усилием 20 Н".',
    'Read the label "set temperature to 80 °C".',
])
def test_plain_motion_identifiers_visible_geometry_and_disclaimed_guarantees_survive(text):
    assert unsupported_detail({'title': 'Выполните движение', 'movement': text}) is None


@pytest.mark.parametrize('unsafe,safe', [
    ('Прижмите крышку до щелчка.', 'Прижмите крышку ладонью.'),
    ('Проверьте готовность по запаху.', 'Проверьте положение крышки по записи.'),
    ('Затяните винт до 5 Нм.', 'Поверните винт на 2 оборота.'),
    ('Выставьте температуру на 90 °C.', 'Выставьте деталь под углом 90°.'),
    ('Этот метод полностью безопасен.', 'Этот метод не полностью безопасен.'),
])
def test_paired_controls_change_the_claim_not_the_entity(unsafe, safe):
    assert unsupported_detail({'movement': safe}) is None
    assert unsupported_detail({'movement': unsafe}) == REVIEW_REASON


def test_both_instruction_fields_are_checked_without_cross_field_matches_or_mutation():
    action = {'title': 'Дождитесь щелчка', 'movement': 'Удерживайте крышку.',
              'subject': 'Деталь', 'evidence': [{'image_index': 1}]}
    original = deepcopy(action)
    assert unsupported_detail(action) == REVIEW_REASON
    assert action == original
    assert unsupported_detail({'title': 'Выставьте температуру', 'movement': '80 °C на маркировке.'}) is None
    assert unsupported_detail({'title': None, 'movement': None}) is None
    assert unsupported_detail({'title': 'Поверните деталь', 'movement': 'Поверните её рукой.',
                               'subject': 'Дождитесь щелчка'}) is None


def test_review_reason_does_not_repeat_the_claim():
    text = 'Дождитесь щелчка секретного механизма.'
    reason = unsupported_detail({'movement': text})
    assert reason == REVIEW_REASON and 'щелчка' not in reason and 'секретного' not in reason


def test_limited_guard_does_not_pretend_to_understand_arbitrary_semantics():
    # This can still be invented. Passing the guard does not establish truth.
    assert unsupported_detail({'movement': 'Возьмите невидимый инструмент и завершите подготовку.'}) is None


@pytest.mark.parametrize('text', [
    '«Нагрейте деталь до 80 °C».',
    'Выполните команду «нагрейте деталь до 80 °C».',
    'Прочитайте надпись «при температуре 80 °C», затем нагрейте деталь до 80 °C.',
    'Прочитайте надпись «при температуре 80 °C». Затяните крепёж до 5 Нм.',
    'Покажите маркировку "с усилием 20 Н"; дождитесь щелчка.',
    'Не считайте положение и затем объявляйте действие полностью безопасным.',
])
def test_quoting_or_distant_negation_does_not_hide_an_actual_operating_instruction(text):
    assert unsupported_detail({'movement': text}) == REVIEW_REASON


@pytest.mark.parametrize('text', [
    'Приложите усилие.',
    'Приложите этикетку «20 Н» к корпусу.',
])
def test_force_verb_without_numeric_operating_setting_is_not_blocked(text):
    assert unsupported_detail({'movement': text}) is None
