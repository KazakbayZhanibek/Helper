"""Local safeguards for instructional prose, not a visual truth detector."""
from copy import deepcopy
import re

from .prose_evidence import REVIEW_REASON as PROSE_REVIEW_REASON


UNCERTAIN_COMMAND_REASON = ('В описании способа выполнения есть сомнение. '
                            'Посмотрите фрагмент и уточните действие, прежде чем добавлять его в инструкцию.')
GROUNDING_REVIEW_REASON = ('Действие не подтверждено согласованными сведениями об указанных кадрах. '
                           'Проверьте предмет, контакт и порядок выполнения по записи.')
_DOUBT = re.compile(r'\b(?:возможно|вероятно|предположительно|кажется|maybe|perhaps|apparently|мүмкін|бәлкім)\b', re.I)


def uncertain_command(observation):
    """Only explicit doubt; no domain/noun/time-specific guesses about pixels."""
    if observation.get('kind') != 'action':
        return False
    text = ' '.join(observation.get(key) or '' for key in ('title', 'movement'))
    return '?' in text or '？' in text or bool(_DOUBT.search(text))


def neutralize_gaps(value, *, trusted_descriptions=()):
    """Never repeat an unverified model claim as the premise of a question.

    Original responses stay in storage. This projection retains frame links,
    reason and timing so the user can inspect the actual recorded interval.
    """
    content = deepcopy(value)
    labels = {
        'sampling_gap': 'Посмотрите промежуток между выбранными кадрами: какое действие действительно показано и как оно выполнено?',
        'unclear_action': 'Уточните короткий переход по записи: какое действие действительно видно, как оно выполнено и чем закончилось?',
        'occluded': 'Проверьте, достаточно ли видны предмет и движение в этом фрагменте. Не восстанавливайте скрытое действие по догадке.',
        'cut': 'Проверьте этот переход по записи: есть ли монтаж и какие действия можно подтвердить?',
        'outside_recording': 'Уточните границы показанной работы. Не добавляйте операции, которых нет в записи.',
    }
    trusted = {UNCERTAIN_COMMAND_REASON, GROUNDING_REVIEW_REASON, PROSE_REVIEW_REASON, *trusted_descriptions}
    for gap in content.get('gaps', []):
        if gap.get('description') not in trusted:
            gap['description'] = labels.get(gap.get('reason'), labels['unclear_action'])
    return content
