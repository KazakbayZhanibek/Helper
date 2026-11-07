"""Conservative display projection. This does not establish visual truth."""
import re

# Only drop a terminal purpose clause naming invisible internal structure.
# Do not truncate after arbitrary «чтобы»: it may contain another action.
_STRUCTURE_PURPOSE = re.compile(
    r',\s*чтобы\s+(?:выработать|усилить|улучшить|укрепить|изменить)\s+'
    r'(?:(?:его|её|ее|их)\s+)?(?:внутреннюю\s+)?структуру'
    r'(?:\s+и\s+сделать\s+(?:его|её|ее|их)?\s*однородн\w*)?\s*([.!?])(?=\s|$)',
    re.IGNORECASE,
)
_GOAL_CUE = re.compile(r'(?<!\w)(?:чтобы|целью|үшін|so\s+that|in\s+order\s+to)(?!\w)', re.IGNORECASE)
_CYRILLIC = re.compile(r'[А-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі]')
_LATIN = re.compile(r'[A-Za-z]')
_WORD = re.compile(r'[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі]+')
_ENGLISH_PHRASE = re.compile(r'\b[a-z]{2,}\s+(?:(?:the|and|out|its|with|for|of|into|was|were|is|are)\s+)[a-z]{2,}(?:\s+[a-z]{2,})*\b', re.IGNORECASE)
_PHOTO_NUMBER_PREFIX = re.compile(r'^\s*(?:на\s+)?(?:фото|кадре|кадр)\s*(?:№\s*)?\d+\s*(?:[:.,—–-]\s*|\s+)', re.IGNORECASE)
_OUTCOME_TITLE = re.compile(r'(?<!\w)(?:формовк\w*|формировани\w*|заверш\w*|финальн\w*|готовност\w*)(?!\w)', re.IGNORECASE)


def review_step(description, photos, *, language, title=''):
    """Return a cautious projection and whether a person must review it.

    Original claims remain in the immutable model response. Frame IDs and
    timestamps are never touched. This narrow heuristic may miss inventions.
    """
    if language != 'ru':
        return description, photos, False
    projected, changes = _STRUCTURE_PURPOSE.subn(lambda match: match.group(1), description)
    needs_review = bool(changes or _GOAL_CUE.search(description) or _OUTCOME_TITLE.search(title))
    cleaned = []
    for photo in photos:
        photo = dict(photo)
        caption = photo.get('caption', '')
        if isinstance(caption, str):
            mixed_word = any(_CYRILLIC.search(word) and _LATIN.search(word) for word in _WORD.findall(caption))
            if mixed_word or (_CYRILLIC.search(caption) and _ENGLISH_PHRASE.search(caption)):
                photo['caption'] = ''
                needs_review = True
            else:
                # Model photo numbers refer to the full batch, not the order
                # in the visible card; remove only an unambiguous prefix.
                cleaned_caption, removed = _PHOTO_NUMBER_PREFIX.subn('', caption, count=1)
                if removed and cleaned_caption.strip():
                    cleaned_caption = cleaned_caption.lstrip()
                    photo['caption'] = cleaned_caption[0].upper() + cleaned_caption[1:]
                    needs_review = True
            if _GOAL_CUE.search(caption):
                needs_review = True
        cleaned.append(photo)
    return projected, cleaned, needs_review
