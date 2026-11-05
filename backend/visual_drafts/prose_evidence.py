"""Narrow text checks for image-only instructional drafts.

These rules detect explicit extra criteria the v2 card has no field to support:
unprovided senses, numeric operating settings and categorical guarantees. They
do not identify objects, infer pixels, validate arbitrary prose, or establish
that a surviving instruction is true. Unknown languages and paraphrases may be
missed deliberately; broad noun/number bans would discard useful movements.
"""
import re
import unicodedata


REVIEW_REASON = (
    'В пояснении есть детали, которые этот анализ кадров не подтверждает. '
    'Сверьте способ выполнения и нужные условия с мастером перед добавлением шага.'
)


def _compile(*expressions):
    return re.compile('|'.join(f'(?:{expression})' for expression in expressions), re.I)


# Negating a prohibited sensory criterion or a guarantee is not asserting it.
# This intentionally handles only an immediately preceding explicit negation.
_NEGATED = re.compile(r'(?:\bне|\bnot|\bnever|\bdo\s+not|\bdon[’\x27]t)\s+$', re.I)
_OBJECT_WORDS = r'(?:(?!(?:и|а|но|затем|and|but|then)\b)[^\W\d_]+\s+){0,3}'
_NEGATIVE_CLICK_LIMIT = re.compile(
    rf'^\s*не\s+(?:затягивайте|нажимайте|давите|дожимайте)\s+{_OBJECT_WORDS}$', re.I)
_NEGATIVE_GUARANTEE = _compile(
    rf'^\s*не\s+(?:считайте|называйте|объявляйте|признавайте)\s+{_OBJECT_WORDS}$',
    rf'^\s*(?:do\s+not|don[’\x27]t|never)\s+(?:call|consider|declare)\s+{_OBJECT_WORDS}$',
)
# Read an explicitly quoted label is an observation task, not an instruction to
# adopt the quoted setting. Require the whole clause: trailing operations cannot
# hide behind this exception. Ordinary quoting alone is never an exemption.
_READ_LABEL = re.compile(
    r'(?:^|[.;])\s*(?:'
    r'(?:прочитайте|покажите)\s+(?:надпись|маркировку|этикетку)|'
    r'(?:read|show)\s+(?:the\s+)?label)\s+'
    r'(?:«(?P<angle>[^«»\n]{1,240})»|"(?P<double>[^"\n]{1,240})")\s*(?=[.;]|$)', re.I)

_SENSORY = _compile(
    r'\b(?:дождитесь|дождаться|ждите)\s+(?:(?:характерного|отчётливого|отчетливого)\s+)?(?:щелчка|звука|запаха)\b',
    r'\bдо\s+(?:(?:характерного|отчётливого|отчетливого)\s+)?щелчка\b',
    r'\bпока\s+(?:вы\s+)?(?:не\s+)?(?:услышите|почувствуете|ощутите|учуете)\b',
    r'\b(?:прислушайтесь|прислушиваться|услышьте)\b',
    r'\b(?:почувствуйте|ощутите)\s+(?:сопротивление|тепло|холод|запах|вибрацию)\b',
    r'\b(?:проверьте|определите|оцените)\s+(?:(?:готовность|результат|состояние|нагрев)\s+)?(?:по\s+запаху|на\s+ощупь)\b',
    r'\b(?:слышен|слышится|раздаётся|раздается)\s+(?:щелчок|звук)\b',
    r'\buntil\s+(?:you\s+)?(?:hear|feel|smell)\b',
    r'\b(?:wait\s+for|listen\s+for)\s+(?:(?:a|the)\s+)?(?:click|sound|smell)\b',
    r'\b(?:check|assess|judge)\s+(?:(?:readiness|the\s+result)\s+)?by\s+(?:smell|touch)\b',
    r'\b(?:шерту\s+дыбысын|шертуді|дыбысты)\s+естігенше\b',
    r'\b(?:қарсылықты|жылуды|дірілді)\s+сезгенше\b',
    r'\bиісіне\s+қарап\s+(?:тексеріңіз|анықтаңыз|бағалаңыз)\b',
)

# Numbers alone, tool identifiers, counts, distances and angles are not evidence
# failures. Require an explicit operating-setting phrase AND a physical unit.
_NUMBER = r'[+−-]?\d+(?:[.,]\d+)?(?:\s*[–-]\s*\d+(?:[.,]\d+)?)?'
_UNIT = r'(?:н\s*[·*⋅]\s*м|н\s+м|нм|n\s*[·*⋅]\s*m|n\s+m|nm|н|n|кпа|мпа|па|kpa|mpa|pa|бар|bar|psi|°\s*[cсfф]|об\s*/\s*мин|rpm)(?!\w)'
_QUANTITY = rf'{_NUMBER}\s*{_UNIT}'
_QUALIFIER = r'(?:(?:ровно|около|до|не\s+менее|не\s+более)\s+)?'
_FEW_WORDS = r'(?:[^\W\d_]+\s+){0,4}'
_SETTINGS = _compile(
    rf'\b(?:приложите|примените)\s+(?:усилие|силу)\s+{_QUALIFIER}{_QUANTITY}',
    rf'\b(?:с|при)\s+(?:усилием|силой|моментом(?:\s+затяжки)?|давлением)\s+{_QUALIFIER}{_QUANTITY}',
    rf'\b(?:установите|выставьте|задайте|поддерживайте|доведите)\s+(?:усилие|силу|момент(?:\s+затяжки)?|давление|температуру|скорость(?:\s+вращения)?|обороты)\s+(?:(?:на|до|в|равным)\s+)?{_QUALIFIER}{_QUANTITY}',
    rf'\b(?:нагрейте|разогрейте|охладите|затяните)\s+{_FEW_WORDS}до\s+{_QUANTITY}',
    rf'\b(?:со\s+скоростью|при\s+температуре)\s+{_QUALIFIER}{_QUANTITY}',
    rf'\b(?:set|maintain)\s+(?:the\s+)?(?:pressure|temperature|speed|torque|force)\s+(?:(?:to|at|of)\s+)?{_QUANTITY}',
    rf'\b(?:heat|cool|tighten)\s+{_FEW_WORDS}(?:to|at)\s+{_QUANTITY}',
    rf'\b(?:температураны|қысымды|күшті|айналу\s+жылдамдығын)\s+{_QUANTITY}\s+(?:етіп\s+)?(?:орнатыңыз|қойыңыз)\b',
)

_GUARANTEE = _compile(
    r'\bгарантирует\s+(?:полную\s+)?(?:безопасность|качество|готовность)\b',
    r'\bгарантированно\s+(?:безопас(?:ен|н\w*)|качествен\w*|готов\w*)\b',
    r'\bполностью\s+безопас(?:ен|н\w*)\b',
    r'\bисключает\s+(?:любой\s+|все\s+)?(?:риск\w*|ошибк\w*|травм\w*)\b',
    r'\bguarantees\s+(?:complete\s+)?(?:safety|quality|readiness)\b',
    r'\b(?:completely|perfectly)\s+safe\b',
    r'\brisk[-\s]free\b',
    r'\bтолық\s+қауіпсіз\b',
    r'\b(?:қауіпсіздікке|сапаға|сапасына)\s+кепілдік\s+береді\b',
)


def unsupported_detail(action):
    """Return a neutral review reason for a narrow explicit claim, else None.

    No text is repaired, copied into the reason, or treated as pixel evidence.
    Only the instructional title and method are checked; metadata is untouched.
    """
    fields = [action.get(key) for key in ('title', 'movement')]
    # Keep a clause boundary between fields so a title cannot supply a verb to
    # an unrelated measurement in the method. NFKC handles full-width digits.
    text = '; '.join(' '.join(unicodedata.normalize('NFKC', value).split())
                     for value in fields if isinstance(value, str))
    labels = [match.span(key) for match in _READ_LABEL.finditer(text)
              for key in ('angle', 'double') if match.group(key) is not None]
    for pattern in (_SENSORY, _SETTINGS, _GUARANTEE):
        for match in pattern.finditer(text):
            if any(start <= match.start() and match.end() <= end for start, end in labels):
                continue
            prefix = text[:match.start()]
            if _NEGATED.search(prefix):
                continue
            clause_prefix = re.split(r'[.;:!?]', prefix)[-1]
            if (pattern is _SENSORY and match.group().lower().startswith('до ')
                    and _NEGATIVE_CLICK_LIMIT.fullmatch(clause_prefix)):
                continue
            if pattern is _GUARANTEE and _NEGATIVE_GUARANTEE.fullmatch(clause_prefix):
                continue
            return REVIEW_REASON
    return None
