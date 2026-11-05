"""Experimental v3 evidence contract; validates references, not visual truth.

The model supplies observations and explicit gaps. Steps are derived from action
fields without a second, independently generated instruction. A valid result
still needs review against the recording: these checks cannot establish that a
claimed movement is visible in the referenced images.
"""
import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from ai_drafts.validation import InvalidModelOutput
from .teaching import STEP_FORMAT, describe_action


SCHEMA = json.loads((Path(__file__).resolve().parents[2] / 'contracts' / 'visual-action-evidence.schema.json').read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
# This experiment has a tighter output bound than the existing v1/v2 contracts.
MAX_OUTPUT_BYTES = 32_768
PHASE_REVIEW_REASON = 'ИИ не смог надёжно связать действие с кадрами. Уточните порядок и способ выполнения по записи.'
STATE_CHANGE_REVIEW_REASON = 'Описаны разные состояния предмета, но действие между ними не выделено. Уточните, что было сделано.'


def _check_payload(value):
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > MAX_OUTPUT_BYTES or next(VALIDATOR.iter_errors(value), None):
            raise InvalidModelOutput('VISUAL_OUTPUT_INVALID')
    except (TypeError, ValueError, RecursionError, UnicodeError) as error:
        raise InvalidModelOutput('VISUAL_OUTPUT_INVALID') from error


def _indices(refs, ordered):
    if any(ref not in ordered for ref in refs):
        raise InvalidModelOutput('VISUAL_EVIDENCE_UNKNOWN')
    indices = [ordered[ref] for ref in refs]
    if indices != list(range(indices[0], indices[-1] + 1)):
        raise InvalidModelOutput('VISUAL_EVIDENCE_ORDER_INVALID')
    return indices


def _times(refs, by_id):
    return {
        'start_ms': min(by_id[ref].coverage_start_ms for ref in refs),
        'end_ms': max(by_id[ref].coverage_end_ms for ref in refs),
    }


def _check_reference_integrity(observation, ordered):
    """Check ownership/order before any recoverable phase validation.

    An invalid phase can occur before the old validator reaches a foreign
    action/after reference. Never let that earlier error hide an untrusted ID.
    """
    _indices(observation['evidence_frame_ids'], ordered)
    if observation['action_frame_ids']:
        _indices(observation['action_frame_ids'], ordered)
    for phase in ('before', 'after'):
        ref = observation[f'{phase}_frame_id']
        if ref is not None and ref not in ordered:
            raise InvalidModelOutput('VISUAL_EVIDENCE_UNKNOWN')
    before_ref, after_ref = observation['before_frame_id'], observation['after_frame_id']
    if before_ref is not None and after_ref is not None and ordered[before_ref] > ordered[after_ref]:
        raise InvalidModelOutput('VISUAL_EVIDENCE_ORDER_INVALID')


def _check_observation(observation, ordered):
    for field in ('subject', 'title', 'before', 'movement', 'after'):
        value = observation[field]
        if value is not None and not value.strip():
            raise InvalidModelOutput('VISUAL_OUTPUT_INVALID')
    refs = observation['evidence_frame_ids']
    evidence_indices = _indices(refs, ordered)
    phase_indices = []
    for phase in ('before', 'after'):
        ref = observation[f'{phase}_frame_id']
        if (observation[phase] is None) != (ref is None):
            raise InvalidModelOutput('VISUAL_EVIDENCE_PHASE_INVALID')
        if ref is not None:
            if ref not in ordered:
                raise InvalidModelOutput('VISUAL_EVIDENCE_UNKNOWN')
            if ref not in refs:
                raise InvalidModelOutput('VISUAL_EVIDENCE_PHASE_INVALID')
            phase_indices.append(ordered[ref])

    before_ref, after_ref = observation['before_frame_id'], observation['after_frame_id']
    if before_ref is not None and after_ref is not None and ordered[before_ref] > ordered[after_ref]:
        raise InvalidModelOutput('VISUAL_EVIDENCE_PHASE_INVALID')
    action_refs = observation['action_frame_ids']
    if observation['kind'] == 'state':
        if observation['movement'] is not None or action_refs or not phase_indices:
            raise InvalidModelOutput('VISUAL_EVIDENCE_PHASE_INVALID')
        # Context frames may support a state without inventing an after phase.
        return None

    if observation['movement'] is None or len(action_refs) < 2:
        raise InvalidModelOutput('VISUAL_EVIDENCE_PHASE_INVALID')
    action_indices = _indices(action_refs, ordered)
    if any(ref not in refs for ref in action_refs):
        raise InvalidModelOutput('VISUAL_EVIDENCE_PHASE_INVALID')
    if before_ref is not None and ordered[before_ref] > action_indices[0]:
        raise InvalidModelOutput('VISUAL_EVIDENCE_PHASE_INVALID')
    if after_ref is not None and ordered[after_ref] < action_indices[-1]:
        raise InvalidModelOutput('VISUAL_EVIDENCE_PHASE_INVALID')
    phase_indices.extend(action_indices)
    if (min(phase_indices), max(phase_indices)) != (evidence_indices[0], evidence_indices[-1]):
        raise InvalidModelOutput('VISUAL_EVIDENCE_PHASE_INVALID')
    return action_indices[0], action_indices[-1]


def _warnings(content, session):
    mandatory = {
        'sampled_frames': 'Анализ выполнен по отдельным кадрам. Невидимые между ними действия могут быть пропущены.',
        'approximate_timing': 'Интервалы приблизительные: проверьте начало и конец каждого шага по полной записи.',
    }
    if session.mode == 'live':
        mandatory['preliminary_live'] = 'Предварительные наблюдения камеры. После сохранения записи выполните анализ видео; эти наблюдения нельзя применить.'
    elif session.asset and (session.window_start_ms > 0 or session.window_end_ms < session.asset.duration_ms):
        mandatory['partial_video'] = 'Проанализирован только выбранный фрагмент видео.'
    for gap in content['gaps']:
        if gap['reason'] == 'occluded':
            mandatory['occluded_view'] = 'Часть процесса закрыта от камеры. Проверьте соответствующие пропуски по записи.'
        elif gap['reason'] in {'cut', 'unclear_action'}:
            mandatory['uncertain_action'] = 'В отмеченных пропусках действие не подтверждено. Проверьте их по полной записи.'
        elif gap['reason'] == 'outside_recording':
            mandatory['partial_video'] = 'Часть процесса находится за пределами записи или выбранного фрагмента.'
        # sampling_gap is already covered by the mandatory sampled_frames code.
    if not content['steps']:
        mandatory['insufficient_evidence'] = 'Наблюдений недостаточно для шагов. Выберите другой ракурс или фрагмент.'
    optional = [warning for warning in content['warnings'] if warning['code'] not in mandatory]
    return optional[:30 - len(mandatory)] + [{'code': code, 'message': message} for code, message in mandatory.items()]


def normalize(value, frames, session):
    """Return a new v3 object with derived steps, times and batch-scoped IDs.

    ``frames`` is the ordered trusted batch of frame objects (id and server
    coverage_start_ms/coverage_end_ms). ``session`` uses the same language, mode
    and optional upload-window attributes as v2; no session ID is required.
    The supplied object remains untouched. The returned object is an internal
    normalized contract, not another model response matching ``SCHEMA``.
    """
    _check_payload(value)
    if value['language'] != session.language:
        raise InvalidModelOutput('VISUAL_LANGUAGE_MISMATCH')
    frames = list(frames)
    by_id = {str(frame.id): frame for frame in frames}
    if len(by_id) != len(frames):
        raise InvalidModelOutput('VISUAL_EVIDENCE_ORDER_INVALID')
    ordered = {str(frame.id): index for index, frame in enumerate(frames)}
    batch = 'b' + hashlib.sha256('\n'.join(ordered).encode('utf-8')).hexdigest()[:32]
    content = copy.deepcopy(value)
    if session.mode == 'upload':
        content['step_format'] = STEP_FORMAT
    if len({observation['id'] for observation in content['observations']}) != len(content['observations']):
        raise InvalidModelOutput('VISUAL_OUTPUT_INVALID')
    if any(not warning['message'].strip() for warning in content['warnings']):
        raise InvalidModelOutput('VISUAL_OUTPUT_INVALID')

    cuts = []
    for index, gap in enumerate(content['gaps'], start=1):
        if not gap['description'].strip():
            raise InvalidModelOutput('VISUAL_OUTPUT_INVALID')
        indices = _indices(gap['evidence_frame_ids'], ordered)
        if gap['reason'] == 'cut':
            cuts.append((indices[0], indices[-1]))
        gap.update(id=f'{batch}:g{index}', **_times(gap['evidence_frame_ids'], by_id))

    review_count = 0

    def add_review_gap(refs, description):
        nonlocal review_count
        review_count += 1
        content['gaps'].append({
            'id': f'{batch}:r{review_count}', 'reason': 'unclear_action',
            'description': description, 'evidence_frame_ids': list(refs),
            **_times(refs, by_id),
        })

    content['steps'] = []
    kept_observations = []
    action_ranges = []
    for observation in content['observations']:
        _check_reference_integrity(observation, ordered)
        try:
            action_range = _check_observation(observation, ordered)
        except InvalidModelOutput as error:
            if session.mode != 'upload' or str(error) != 'VISUAL_EVIDENCE_PHASE_INVALID':
                raise
            # Discard the entire unsupported claim. Its trusted context can
            # locate a question, but must not be repaired into an action.
            add_review_gap(observation['evidence_frame_ids'], PHASE_REVIEW_REASON)
            continue
        observation['id'] = f"{batch}:{observation['id']}"
        observation.update(_times(observation['evidence_frame_ids'], by_id))
        kept_observations.append(observation)
        if action_range is None:
            continue
        if any(action_range[0] <= start and action_range[1] >= end for start, end in cuts):
            raise InvalidModelOutput('VISUAL_EVIDENCE_CUT_INVALID')
        action_ranges.append(action_range)
        content['steps'].append({
            'title': observation['title'],
            'description': describe_action(observation, language=session.language, step_format=content.get('step_format')),
            'evidence_frame_ids': list(observation['action_frame_ids']),
            'observation_ids': [observation['id']],
            **_times(observation['action_frame_ids'], by_id),
        })
    content['observations'] = kept_observations
    if session.mode == 'upload':
        for observation in kept_observations:
            if observation['kind'] != 'state' or observation['before'] is None or observation['after'] is None:
                continue
            before_index = ordered[observation['before_frame_id']]
            after_index = ordered[observation['after_frame_id']]
            # This text heuristic raises a review question, not proof of a
            # change: paraphrases may also differ. Never infer a missing action.
            before_text = ' '.join(observation['before'].casefold().split())
            after_text = ' '.join(observation['after'].casefold().split())
            if before_index == after_index or before_text == after_text:
                continue
            if any(start <= before_index and end >= after_index for start, end in action_ranges):
                continue
            refs = [str(frame.id) for frame in frames[before_index:after_index + 1]]
            add_review_gap(refs, STATE_CHANGE_REVIEW_REASON)
    content['warnings'] = _warnings(content, session)
    return content
