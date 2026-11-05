"""Render a teachable step from one observation without generating new facts.

The visible before/after states are context and a visual comparison, not proof
that the demonstrated technique is safe or that a quality target was achieved.
This renderer cannot repair an observation that misidentifies an action.
"""
from copy import deepcopy

STEP_FORMAT = 'teaching_v1'
PRESENTATION_FORMAT = 'process_story_v1'
PROCESS_STYLE = '''Объясняй процесс как мастер рядом с учеником: «замешиваем», «складываем», «затем переходим», на языке запроса. title — понятный этап работы. В movement (или description) дай 2–4 коротких связанных предложения: что сейчас делаем, как именно, что видимо меняется. Один этап объединяет повторяющиеся движения; не дели его на карточки контакта и удержания. Сохраняй самостоятельные короткие операции. Связки «сначала/затем» используй лишь для показанного порядка. Не пиши «предмет виден», «наблюдается контакт», не повторяй название вместо объяснения. Не придумывай невидимые прошлые/будущие этапы, назначение или готовность. Например, при видимом складывании теста объясни движение руками, а не «готовим тесто для готовки». Это правило для любых работ, не рецепт.'''
LABELS = {
    'ru': {
        'subject': 'С чем работаем',
        'before': 'Перед началом на видео',
        'movement': 'Как выполнить',
        'after': 'Что сравнить с видео после действия',
        'unknown_after': 'Результат на этих кадрах не показан. Уточните у мастера, что проверить после действия.',
    },
    'kk': {
        'subject': 'Немен жұмыс істейміз',
        'before': 'Видеодағы бастапқы күй',
        'movement': 'Қалай орындау керек',
        'after': 'Әрекеттен кейін видеомен нені салыстыру керек',
        'unknown_after': 'Бұл кадрларда нәтиже көрсетілмеген. Әрекеттен кейін нені тексеру керегін шеберден нақтылаңыз.',
    },
}


def describe_action(observation, *, language, step_format=None):
    """Return an exact, bounded projection; missing format means historical v3."""
    if observation['kind'] != 'action' or not observation.get('movement'):
        raise ValueError('Only action observations can become instructional steps')
    if step_format is None:
        return observation['movement']
    if step_format != STEP_FORMAT or language not in {'ru', 'kk', 'mixed'}:
        raise ValueError('Unknown teaching format or language')
    # Mixed instructions retain their source text; section labels follow the
    # Russian interface. No translation or interpretation is performed here.
    labels = LABELS['kk' if language == 'kk' else 'ru']
    sections = [f"{labels['subject']}\n{observation['subject']}"]
    if observation['before'] is not None:
        sections.append(f"{labels['before']}\n{observation['before']}")
    sections.append(f"{labels['movement']}\n{observation['movement']}")
    sections.append(f"{labels['after']}\n{observation['after'] if observation['after'] is not None else labels['unknown_after']}")
    description = '\n\n'.join(sections)
    # The schema's bounded source fields fit naturally; never silently truncate
    # a method or a missing-result notice if a future contract becomes larger.
    if len(description) > 2000:
        raise ValueError('Instructional step exceeds the working-copy limit')
    return description


def present_instruction(content):
    """Learner-facing text after validation, separate from the archived contract.

    The old format remains the exact evidence projection used by all validators.
    Its per-frame facts are available in the review drawer, not inserted into
    the learner's explanation. This function neither rewrites prose nor infers
    missing actions. Read and apply must use it after the same validation gates.
    """
    result = deepcopy(content)
    if content.get('schema_version') != '3' or content.get('step_format') != STEP_FORMAT:
        return result
    observations = {item['id']: item for item in content['observations']}
    for step in result['steps']:
        linked = [observations[ref] for ref in step['observation_ids']]
        if not linked or any(describe_action(item, language=content['language'], step_format=STEP_FORMAT)
                             != step['description'] for item in linked):
            raise ValueError('Instruction presentation requires a validated projection')
        # Aggregated identities may describe the same method. Never join
        # different methods or manufacture a transition from overlapping times.
        methods = {item['movement'] for item in linked}
        if len(methods) != 1:
            raise ValueError('Instruction presentation requires one supported method')
        step['description'] = linked[0]['movement']
    result['presentation_format'] = PRESENTATION_FORMAT
    return result
