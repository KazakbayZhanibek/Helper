"""Account for every sampled transition before composing teaching steps.

One model response, not an independent verifier. Coverage is bookkeeping over
the supplied images; it cannot prove visual truth or reveal unsampled motion.
"""
from copy import deepcopy
import hashlib
import json
import uuid

from jsonschema import Draft202012Validator, FormatChecker

from ai_drafts.providers import ProviderFailure
from ai_drafts.validation import InvalidModelOutput
from .action_evidence import SCHEMA as ACTION_SCHEMA, MAX_OUTPUT_BYTES, normalize, _warnings
from .instruction_checks import uncertain_command, neutralize_gaps, UNCERTAIN_COMMAND_REASON

VERSION = 'v1'
WIRE_VERSION = '3-short-actions-v1'
ACTION_LINK_REVIEW_REASON = ('Объяснение действия не согласовано с указанными переходами между кадрами. '
                             'Посмотрите этот фрагмент и уточните, какое действие действительно показано и как его выполнить.')
SCHEMA = deepcopy(ACTION_SCHEMA)
SCHEMA['$id'] = 'urn:master-experience:visual-short-actions:v1'
SCHEMA['properties']['schema_version']['enum'] = [WIRE_VERSION]
SCHEMA['properties']['transitions'] = {
    'type': 'array', 'minItems': 1, 'maxItems': 11,
    'items': {'type': 'object', 'additionalProperties': False,
              'required': ['pair_index', 'change', 'description', 'observation_ids'],
              'properties': {
                  'pair_index': {'type': 'integer', 'minimum': 0, 'maximum': 10},
                  'change': {'type': 'string', 'enum': ['visible_action', 'unexplained_change', 'no_clear_change', 'cut']},
                  'description': {'type': 'string', 'minLength': 1, 'maxLength': 240},
                  'observation_ids': {'type': 'array', 'maxItems': 12, 'uniqueItems': True,
                                      'items': {'type': 'string', 'enum': [f'o{i}' for i in range(1, 13)]}},
              }},
}
SCHEMA['required'].append('transitions')
# Keep the factual temporal inventory before the instructional synthesis.
SCHEMA['properties'] = {name: SCHEMA['properties'][name] for name in (
    'schema_version', 'language', 'transitions', 'observations', 'gaps', 'warnings')}
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())

SYSTEM = '''Сначала составь transitions по ВСЕМ соседним парам из temporal_pairs в их порядке, затем observations и gaps. Это один разбор изображений, не независимая проверка достоверности.
Для каждой пары верни pair_index, change, краткое фактическое description (какой предмет/рука был где и как изменился контакт или положение) и observation_ids. visible_action означает различимый способ перехода; свяжи его с конкретным action. unexplained_change — состояние изменилось, а механизм не виден; no_clear_change — различимого изменения нет; cut — монтаж. Все pair_index обязательны ровно один раз. Интервал gap_ms — расстояние между кадрами, не длительность операции. Не объявляй непрерывное наблюдение при любом его значении.
Короткое действие может подтверждаться исходным положением предмета в руке и следующим контактом с опорой либо движением от неё. Минимум два action_frame_ids — это кадры, совместно подтверждающие видимый переход; не требуются два кадра в середине движения. До/после могут совпадать с границами action_frame_ids. Если видны только отсутствие и наличие без механизма, оставь вопрос, а не придуманный перенос. Сама по себе движущаяся рука у неподвижного предмета не доказывает перемещение предмета.
После таблицы составь учебную последовательность ВСЕЙ показанной работы. Включи видимые короткие переходы до, между и после крупных операций. Их можно объединить с соседним шагом, но movement должен объяснить сам переход и его место в порядке действий. Не заменяй внесение, опускание, унос или смену захвата словами «появилось/находится/положение изменилось». Не дописывай скрытое действие ради полноты. Сначала укажи видимый способ, затем наблюдаемый результат либо что нужно уточнить. Повторяющийся цикл объединяй только после проверки всех пар; сохрани различимые движения цикла.
Верни schema_version="3-short-actions-v1". observations, gaps и warnings соблюдают прежние ограничения v3; transitions содержит по одной записи на каждую temporal_pairs. Ссылки observations — o1..o12; временные интервалы не возвращай. Весь ответ должен поместиться в 32 KiB UTF-8.'''


def temporal_pairs(frames):
    """Trusted timestamps only; never infer event semantics from image differences."""
    try:
        if not 2 <= len(frames) <= 12:
            raise ValueError()
        ids, times, sequences = [], [], []
        for frame in frames:
            ref = str(uuid.UUID(str(frame['id'])))
            timestamp, sequence = frame['timestamp_ms'], frame['sequence']
            if (ref != str(frame['id']) or type(timestamp) is not int or not 0 <= timestamp <= 600000
                    or type(sequence) is not int or sequence < 0):
                raise ValueError()
            ids.append(ref)
            times.append(timestamp)
            sequences.append(sequence)
        if (len(set(ids)) != len(ids) or any(a >= b for a, b in zip(times, times[1:]))
                or any(a >= b for a, b in zip(sequences, sequences[1:]))):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ProviderFailure('VISUAL_EVIDENCE_INVALID', 'Неверная последовательность кадров для разбора коротких действий.') from None
    return [{'pair_index': i, 'from_frame_id': left, 'to_frame_id': right, 'gap_ms': times[i + 1] - times[i]}
            for i, (left, right) in enumerate(zip(ids, ids[1:]))]


def normalize_short_actions(value, frames, session):
    try:
        if (not isinstance(value, dict) or len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_OUTPUT_BYTES
                or next(VALIDATOR.iter_errors(value), None)):
            raise InvalidModelOutput('VISUAL_OUTPUT_INVALID')
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise InvalidModelOutput('VISUAL_OUTPUT_INVALID') from None
    if session.mode != 'upload':
        raise InvalidModelOutput('VISUAL_PROTOCOL_INVALID')
    frames = list(frames)
    try:
        pairs = temporal_pairs([{'id': str(frame.id), 'sequence': frame.sequence, 'timestamp_ms': frame.timestamp_ms} for frame in frames])
    except ProviderFailure:
        raise InvalidModelOutput('VISUAL_EVIDENCE_ORDER_INVALID') from None
    transitions = value['transitions']
    if [row['pair_index'] for row in transitions] != list(range(len(pairs))):
        raise InvalidModelOutput('VISUAL_TRANSITION_COVERAGE_INVALID')
    observations = {item['id']: item for item in value['observations']}
    for row in transitions:
        if not row['description'].strip() or any(ref not in observations for ref in row['observation_ids']):
            raise InvalidModelOutput('VISUAL_TRANSITION_LINK_INVALID')
    wire = deepcopy(value)
    wire.pop('transitions')
    wire['schema_version'] = '3'
    content = normalize(wire, frames, session)
    kept = {item['id'].split(':')[-1]: item for item in content['observations']}
    content['temporal_coverage'] = {
        'version': VERSION, 'unresolved_pairs': 0,
        'pairs': [{**pair, 'change': row['change'], 'description': row['description'],
                   'observation_ids': [kept[ref]['id'] for ref in row['observation_ids'] if ref in kept]}
                  for pair, row in zip(pairs, transitions)],
    }
    return reconcile_action_links(content, frames, session)


def reconcile_action_links(value, frames, session):
    """Read-only projection for fresh and stored v1 results; never rewrites history.

    Corrupt references and cut violations fail before unsupported claims can be
    removed. Multiple identities on an identical aggregate step are kept when
    at least one of those identities has its own matching visible transition.
    """
    content = deepcopy(value)
    if content.get('schema_version') != '3' or content.get('temporal_coverage', {}).get('version') != VERSION:
        return content
    if session.mode != 'upload':
        raise InvalidModelOutput('VISUAL_PROTOCOL_INVALID')
    frames = list(frames)
    by_id = {str(frame.id): frame for frame in frames}
    if len(by_id) != len(frames):
        raise InvalidModelOutput('VISUAL_EVIDENCE_ORDER_INVALID')
    ordered = {str(frame.id): index for index, frame in enumerate(sorted(frames, key=lambda frame: frame.sequence))}
    from .action_evidence import _check_reference_integrity, _check_observation
    from .teaching import describe_action
    try:
        kept = {item['id']: item for item in content['observations']}
        if len(kept) != len(content['observations']):
            raise InvalidModelOutput('VISUAL_OUTPUT_INVALID')
        for item in kept.values():
            _check_reference_integrity(item, ordered)
            _check_observation(item, ordered)
        linked_ids = set()
        for step in content['steps']:
            links, refs = step['observation_ids'], step['evidence_frame_ids']
            if not links or len(links) != len(set(links)):
                raise InvalidModelOutput('VISUAL_TRANSITION_LINK_INVALID')
            for ref in links:
                item = kept[ref]
                if (item['kind'] != 'action' or item['action_frame_ids'] != refs
                        or item['title'] != step['title']
                        or describe_action(item, language=content['language'], step_format=content.get('step_format')) != step['description']):
                    raise InvalidModelOutput('VISUAL_TRANSITION_LINK_INVALID')
                linked_ids.add(ref)
            if (step['start_ms'] != min(by_id[ref].coverage_start_ms for ref in refs)
                    or step['end_ms'] != max(by_id[ref].coverage_end_ms for ref in refs)):
                raise InvalidModelOutput('VISUAL_EVIDENCE_ORDER_INVALID')
        if linked_ids != {ref for ref, item in kept.items() if item['kind'] == 'action'}:
            raise InvalidModelOutput('VISUAL_TRANSITION_LINK_INVALID')
        for gap in content['gaps']:
            refs = gap['evidence_frame_ids']
            if not refs or any(ref not in by_id for ref in refs):
                raise InvalidModelOutput('VISUAL_EVIDENCE_UNKNOWN')
            if gap['reason'] == 'cut' and any(
                    item['kind'] == 'action'
                    and ordered[item['action_frame_ids'][0]] <= min(ordered[ref] for ref in refs)
                    and ordered[item['action_frame_ids'][-1]] >= max(ordered[ref] for ref in refs)
                    for item in kept.values()):
                raise InvalidModelOutput('VISUAL_EVIDENCE_CUT_INVALID')
        rows = content['temporal_coverage']['pairs']
        groups = {}
        for row in rows:
            refs = [row['from_frame_id'], row['to_frame_id']]
            if any(ref not in by_id for ref in refs):
                raise InvalidModelOutput('VISUAL_EVIDENCE_UNKNOWN')
            left, right = (by_id[ref] for ref in refs)
            if (left.sequence >= right.sequence or left.timestamp_ms >= right.timestamp_ms
                    or type(row['gap_ms']) is not int or row['gap_ms'] != right.timestamp_ms - left.timestamp_ms):
                raise InvalidModelOutput('VISUAL_EVIDENCE_ORDER_INVALID')
            if (row['change'] not in {'visible_action', 'unexplained_change', 'no_clear_change', 'cut'}
                    or not isinstance(row['description'], str) or not row['description'].strip()
                    or type(row['pair_index']) is not int
                    or len(row['observation_ids']) != len(set(row['observation_ids']))
                    or any(ref not in kept for ref in row['observation_ids'])):
                raise InvalidModelOutput('VISUAL_TRANSITION_LINK_INVALID')
            group = groups.setdefault(row.get('window_id'), [])
            if row['pair_index'] != len(group) or (group and group[-1]['to_frame_id'] != row['from_frame_id']):
                raise InvalidModelOutput('VISUAL_TRANSITION_COVERAGE_INVALID')
            group.append(row)
            # Preserve fail-closed behavior before discarding unlinked actions.
            if row['change'] == 'cut' and any(item['kind'] == 'action' and all(ref in item['action_frame_ids'] for ref in refs) for item in kept.values()):
                raise InvalidModelOutput('VISUAL_EVIDENCE_CUT_INVALID')
        if not rows:
            raise InvalidModelOutput('VISUAL_TRANSITION_COVERAGE_INVALID')
    except InvalidModelOutput:
        raise
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        raise InvalidModelOutput('VISUAL_OUTPUT_INVALID') from None

    # The two directions of the relation must agree. This is consistency of
    # model claims, not independent verification of what an image depicts.
    supported = {
        ref for row in rows if row['change'] == 'visible_action'
        for ref in row['observation_ids'] if kept[ref]['kind'] == 'action'
        and all(frame_id in kept[ref]['action_frame_ids'] for frame_id in (row['from_frame_id'], row['to_frame_id']))
    }
    removed = {ref: item for ref, item in kept.items()
               if item['kind'] == 'action' and (ref not in supported or uncertain_command(item))}
    content['observations'] = [item for item in content['observations'] if item['id'] not in removed]
    steps, retained_equivalents = [], set()
    for step in content['steps']:
        links = [ref for ref in step['observation_ids'] if ref not in removed]
        if links:
            steps.append({**step, 'observation_ids': links})
            retained_equivalents.update(ref for ref in step['observation_ids'] if ref in removed)
    content['steps'] = steps

    def question(refs, identity, description, reason='unclear_action'):
        content['gaps'].append({'id': identity, 'reason': reason, 'description': description,
                               'evidence_frame_ids': list(refs),
                               'start_ms': min(by_id[ref].coverage_start_ms for ref in refs),
                               'end_ms': max(by_id[ref].coverage_end_ms for ref in refs)})

    for ref, item in removed.items():
        if ref in retained_equivalents:
            continue
        refs = item['action_frame_ids']
        reason = UNCERTAIN_COMMAND_REASON if uncertain_command(item) else ACTION_LINK_REVIEW_REASON
        if not any(gap['description'] == reason and gap['evidence_frame_ids'] == refs for gap in content['gaps']):
            question(refs, f'{ref}:unlinked', reason)
    kept = {ref: item for ref, item in kept.items() if ref not in removed}
    audit, unresolved = [], 0
    for row in rows:
        refs = [row['from_frame_id'], row['to_frame_id']]
        links = [kept[ref] for ref in row['observation_ids'] if ref in kept]
        linked_action = any(item['kind'] == 'action' and all(ref in item['action_frame_ids'] for ref in refs) for item in links)
        unsupported_pair = not linked_action and any(all(ref in item['action_frame_ids'] for ref in refs) for identity, item in removed.items() if identity not in retained_equivalents)
        # Once the unsupported imperative is removed, its unresolved marker must
        # survive a second projection instead of reverting to no_clear_change.
        retained_question = row['change'] == 'no_clear_change' and row.get('status') == 'unresolved'
        missing = row['change'] == 'unexplained_change' or (row['change'] == 'visible_action' and not linked_action) or unsupported_pair or retained_question
        status = 'linked_action' if row['change'] == 'visible_action' and linked_action else 'unresolved' if missing else row['change']
        audit.append({**row, 'status': status, 'observation_ids': [item['id'] for item in links]})
        if not missing and row['change'] != 'cut':
            continue
        unresolved += int(missing)
        rejected_link = any(ref in removed for ref in row['observation_ids'])
        if missing and rejected_link:
            for gap in content['gaps']:
                if (gap['reason'] == 'unclear_action' and gap['evidence_frame_ids'] == refs
                        and gap['description'].startswith('Уточните короткий переход по записи:')):
                    # Older projections copied the rejected model claim into
                    # a question. Neutralize that derived label, not raw history.
                    gap['description'] = ACTION_LINK_REVIEW_REASON
        if any(all(ref in gap['evidence_frame_ids'] for ref in refs) for gap in content['gaps']):
            continue
        description = (ACTION_LINK_REVIEW_REASON if missing and (unsupported_pair or retained_question or rejected_link)
                       else ('Уточните короткий переход по записи: ' if missing else 'Проверьте монтажный переход: ') + row['description'])
        identity = 'transition:' + hashlib.sha256('\n'.join(refs).encode()).hexdigest()[:32]
        question(refs, identity, description, 'unclear_action' if missing else 'cut')
    content['temporal_coverage'] = {**content['temporal_coverage'], 'pairs': audit, 'unresolved_pairs': unresolved}
    content['warnings'] = _warnings(content, session)
    from .action_evidence import PHASE_REVIEW_REASON, STATE_CHANGE_REVIEW_REASON
    return neutralize_gaps(content, trusted_descriptions=(ACTION_LINK_REVIEW_REASON, PHASE_REVIEW_REASON, STATE_CHANGE_REVIEW_REASON))
