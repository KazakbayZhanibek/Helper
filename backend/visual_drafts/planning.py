"""Deterministic temporal coverage, with one bounded refinement level."""
from copy import deepcopy
import json
import uuid

OVERVIEW_WINDOW_MS = 30000
REFINEMENT_WINDOW_MS = 15000
REFINEMENT_CODES = frozenset({'uncertain_action', 'occluded_view', 'insufficient_evidence', 'missing_parameters'})
REFINABLE_GAPS = frozenset({'occluded', 'sampling_gap', 'unclear_action'})
IRRECOVERABLE_GAPS = frozenset({'cut', 'outside_recording'})


def intervals(start_ms, end_ms, width=OVERVIEW_WINDOW_MS):
    return [(start, min(start + width, end_ms)) for start in range(start_ms, end_ms, width)]


def needs_refinement(content):
    if content.get('frame_grounding', {}).get('version') in ('v1', 'v2'):
        # This mode improves verification inside the same call allowance.
        # Unknown facts require human review, never an automatic extra pass.
        return False
    if content.get('schema_version') == '3':
        gaps = content.get('gaps', [])
        if any(gap.get('reason') in REFINABLE_GAPS for gap in gaps):
            return True
        # More samples cannot recover an edit or an action outside the recording.
        # Generic warnings (including no steps) must not spend calls on that gap.
        if gaps and all(gap.get('reason') in IRRECOVERABLE_GAPS for gap in gaps):
            return False
    return not content.get('steps') or any(w.get('code') in REFINEMENT_CODES for w in content.get('warnings', []))


def _frame_fingerprints(session, windows):
    """Resolve persisted model-image identities in one tenant/session scoped read."""
    if not getattr(session, 'id', None) or not getattr(session, 'organization_id', None):
        return {}
    ids = set()
    for window in windows:
        refs = getattr(window, 'frame_ids', None)
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, str):
                continue
            try:
                ids.add(uuid.UUID(ref))
            except ValueError:
                continue
    if not ids:
        return {}
    from .models import FrameEvidence
    rows = FrameEvidence.objects.filter(
        id__in=ids, session_id=session.id, organization_id=session.organization_id,
        session__organization_id=session.organization_id, session__frames_purged_at__isnull=True,
    ).values_list('id', 'timestamp_ms', 'sha256')
    return {str(ref): (timestamp, sha256) for ref, timestamp, sha256 in rows
            if type(timestamp) is int and timestamp >= 0 and isinstance(sha256, str)
            and len(sha256) == 64 and all(char in '0123456789abcdef' for char in sha256)}


def _window_fingerprints(window, resolved):
    refs = getattr(window, 'frame_ids', None)
    if (not isinstance(refs, list) or not 2 <= len(refs) <= 12
            or any(not isinstance(ref, str) for ref in refs)
            or len(refs) != len(set(refs)) or any(ref not in resolved for ref in refs)):
        return None
    return {resolved[ref] for ref in refs}


def _completed_coverage_partition(session, parent, children, fingerprints, *, checking=False):
    """Retain the entire newer evidence set, including an explicit lack of actions.

    Completed windows are already validated by the worker. Check their pinned
    format and full temporal coverage before preferring them over the overview;
    neither a nonempty step list nor missing uncertainty is a quality test.
    Every original model-image timestamp and JPEG hash must also survive in
    the new batches: denser sampling alone can lose a brief visible action.
    """
    from .review_worker import effective_content, review_state
    if len(children) < 2:
        return False
    left, right = getattr(parent, 'start_ms', None), getattr(parent, 'end_ms', None)
    if type(left) is not int or type(right) is not int or left >= right:
        return False
    parts = []
    for child in children:
        start, end = getattr(child, 'start_ms', None), getattr(child, 'end_ms', None)
        if (child.state != 'completed' or type(start) is not int or type(end) is not int
                or not left <= start < end <= right
                or (checking and review_state(child) != 'completed')):
            return False
        content = effective_content(child)
        if (not isinstance(content, dict) or content.get('schema_version') != '3'
                or content.get('step_format') != 'teaching_v1'
                or content.get('language') != session.language
                or any(not isinstance(content.get(key), list) for key in ('steps', 'observations', 'gaps', 'warnings'))):
            return False
        coverage = content.get('temporal_coverage')
        if (not isinstance(coverage, dict) or coverage.get('version') != 'v1'
                or not isinstance(coverage.get('pairs'), list) or not coverage['pairs']
                or type(coverage.get('unresolved_pairs')) is not int or coverage['unresolved_pairs'] < 0):
            return False
        if any(not isinstance(pair, dict) or any(key not in pair for key in (
                'pair_index', 'from_frame_id', 'to_frame_id', 'gap_ms', 'change', 'status', 'observation_ids'))
               for pair in coverage['pairs']):
            return False
        parts.append((start, end))
    parts.sort()
    if not (parts[0][0] == left and parts[-1][1] == right
            and all(previous[1] == current[0] for previous, current in zip(parts, parts[1:]))):
        return False
    original = _window_fingerprints(parent, fingerprints)
    refined = [_window_fingerprints(child, fingerprints) for child in children]
    return (original is not None and all(part is not None for part in refined)
            and original.issubset(set().union(*refined)))


def analysis_data(session, *, budget=True):
    from .review_worker import enabled as review_enabled, review_state
    windows = list(session.analysis_windows.select_related('semantic_review'))
    overview = [window for window in windows if window.parent_id is None]
    refined = [window for window in windows if window.parent_id is not None]
    missing = [{'start_ms': window.start_ms, 'end_ms': window.end_ms} for window in overview if window.state != 'completed']
    result = {
        'scope': session.analysis_scope,
        'windows_total': len(overview),
        'windows_completed': sum(window.state == 'completed' for window in overview),
        'refinement_windows': len(refined),
        'refinements_completed': sum(window.state == 'completed' for window in refined),
        'coverage_complete': bool(overview) and not missing,
        'sampling': 'discrete_frames',
        'unprocessed_ranges': missing,
        'windows': [{'id': str(window.id), 'start_ms': window.start_ms, 'end_ms': window.end_ms, 'kind': 'refinement' if window.parent_id else 'overview', 'state': window.state, 'error_code': window.error_code or None} for window in windows],
    }
    if review_enabled(session):
        candidates = [window for window in windows if window.state == 'completed']
        result['semantic_review'] = {
            'strategy': 'v1', 'windows_eligible': len(candidates),
            'windows_checked': sum(review_state(window) == 'completed' for window in candidates),
            'windows_unchecked': sum(review_state(window) != 'completed' for window in candidates),
        }
    if budget:
        from ai_budget.services import budget_snapshot
        result['budget'] = budget_snapshot(session)
    return result


def aggregate_content(session, windows, *, stop_code=''):
    """Use refinements only when every child succeeded; retain the overview otherwise."""
    from .review_worker import enabled as review_enabled, review_state, effective_content
    overview = [window for window in windows if window.parent_id is None]
    steps, warnings, observations, gaps = [], [], [], []
    temporal_pairs, temporal_unresolved = [], 0
    grounding_batches = {}
    structured = getattr(session, 'analysis_budget', {}).get('visual_protocol') == '3'
    step_formats = set()
    unresolved_refinements = False
    checking = review_enabled(session) if hasattr(session, 'mode') else False
    budget = getattr(session, 'analysis_budget', {})
    grounding_pin = budget.get('frame_grounding')
    if grounding_pin not in (None, 'v1', 'v2') or (grounding_pin and (
            getattr(session, 'mode', None) != 'upload' or budget.get('visual_protocol') != '3'
            or budget.get('short_action_coverage') != 'v1' or budget.get('semantic_review'))):
        from media_pipeline.exceptions import ProcessingFailure
        raise ProcessingFailure('VISUAL_PROTOCOL_INVALID', 'Неизвестный режим проверки фактов кадров.')
    preserve_refined_evidence = (getattr(session, 'mode', None) == 'upload'
                                 and budget.get('visual_protocol') == '3'
                                 and budget.get('short_action_coverage') == 'v1')
    fingerprints = (_frame_fingerprints(session, windows)
                    if preserve_refined_evidence and any(window.parent_id is not None for window in windows) else {})
    selected_for_review = []
    for window in overview:
        if window.state != 'completed':
            continue
        children = [child for child in windows if child.parent_id == window.id]
        if preserve_refined_evidence:
            refined = _completed_coverage_partition(session, window, children, fingerprints, checking=checking)
        else:
            # Preserve historical selection for unpinned/legacy analysis modes.
            refined = bool(children) and all(child.state == 'completed' and effective_content(child).get('steps') and not needs_refinement(effective_content(child))
                                            and (not checking or review_state(child) == 'completed') for child in children)
        # A partial or failed new set cannot replace a full overview. In the
        # coverage mode, a complete uncertain result must not resurrect older
        # commands merely because those commands were worded confidently.
        if children and not refined:
            unresolved_refinements = True
        selected = children if refined else [window]
        selected_for_review.extend(selected)
        for selected_window in selected:
            content = deepcopy(effective_content(selected_window))
            grounding = content.get('frame_grounding')
            if (grounding is not None and not isinstance(grounding, dict)) or (grounding or {}).get('version') != grounding_pin:
                from media_pipeline.exceptions import ProcessingFailure
                raise ProcessingFailure('VISUAL_PROTOCOL_INVALID', 'Фрагмент использует другой режим проверки кадров.')
            if grounding_pin:
                batches = grounding.get('batches')
                if not isinstance(batches, list):
                    from media_pipeline.exceptions import ProcessingFailure
                    raise ProcessingFailure('VISUAL_PROTOCOL_INVALID', 'Проверка кадров не содержит исходных фактов.')
                for batch in batches:
                    required = (('frame_ids', 'actions', 'cut_after_image_indices', 'rejected_observation_ids')
                                if grounding_pin == 'v2' else ('entities', 'states', 'observation_links'))
                    if (not isinstance(batch, dict) or not isinstance(batch.get('id'), str) or not batch['id']
                            or any(name not in batch for name in required)
                            or (grounding_pin == 'v2' and any(not isinstance(batch[name], list) for name in required))
                            or (batch['id'] in grounding_batches and grounding_batches[batch['id']] != batch)):
                        from media_pipeline.exceptions import ProcessingFailure
                        raise ProcessingFailure('VISUAL_PROTOCOL_INVALID', 'Проверка кадров содержит несовместимые факты.')
                    # Keep even batches whose actions were rejected: the local
                    # verifier needs their unknown/conflicting states on read.
                    grounding_batches[batch['id']] = batch
            coverage = content.get('temporal_coverage', {})
            if coverage.get('version') == 'v1':
                temporal_pairs.extend({**pair, 'window_id': str(selected_window.id)} for pair in coverage['pairs'])
                temporal_unresolved += coverage['unresolved_pairs']
            steps.extend(content.get('steps', []))
            warnings.extend(content.get('warnings', []))
            if content.get('schema_version') == '3':
                structured = True
                step_formats.add(content.get('step_format'))
                observations.extend(content.get('observations', []))
                gaps.extend(content.get('gaps', []))
            elif content.get('steps'):
                step_formats.add(None)
    steps.sort(key=lambda step: (step['start_ms'], step['end_ms']))
    # Deduplicate identical evidence only. Similar text in adjacent windows can be
    # two real repeated actions and must not be silently merged by a heuristic.
    seen_steps, unique_steps = {}, []
    for step in steps:
        key = (step['title'], step['description'], tuple(step['evidence_frame_ids']))
        if key not in seen_steps:
            seen_steps[key] = step
            unique_steps.append(step)
        elif 'observation_ids' in seen_steps[key] and 'observation_ids' in step:
            kept = seen_steps[key]['observation_ids']
            kept.extend(ref for ref in step['observation_ids'] if ref not in kept)
        elif 'observation_ids' not in step:
            # A mixed legacy result remains reviewable, but must not acquire
            # action provenance that its original v2 answer never supplied.
            seen_steps[key].pop('observation_ids', None)
    missing = [window for window in overview if window.state != 'completed']
    if missing:
        warnings.append({'code': 'incomplete_coverage', 'message': 'Разобрана только часть записи. Непросмотренные интервалы перечислены в результате; готовые наблюдения сохранены.'})
    if checking:
        checked = sum(review_state(window) == 'completed' for window in selected_for_review)
        if checked:
            warnings.append({'code': 'semantic_review_completed', 'message': f'Повторно сопоставлены с кадрами {checked} фрагментов. Это проверка той же моделью, которая тоже может ошибаться; мастер должен проверить действия и способ выполнения.'})
        if checked < len(selected_for_review):
            warnings.append({'code': 'semantic_review_incomplete', 'message': 'Часть шагов не прошла повторное сопоставление с кадрами: не хватило лимита или проверка не завершилась. Первоначальные предложения сохранены и требуют проверки мастером.'})
    if stop_code:
        warnings.append({'code': 'budget_limit_reached' if stop_code.startswith('AI_BUDGET') or stop_code == 'AI_CALL_LIMIT' else 'analysis_interrupted', 'message': 'Дополнительные обращения остановлены. Сохранены только уже проверенные ответы; непроверенные моменты требуют просмотра человеком.'})
    if unresolved_refinements:
        warnings.append({'code': 'refinement_unresolved', 'message': 'Дополнительный просмотр не дал достаточно уверенного уточнения. Сохранено исходное наблюдение; проверьте этот момент по видео.'})
    unresolved = any(window.state == 'completed' and needs_refinement(effective_content(window)) and not any(child.parent_id == window.id for child in windows) for window in overview)
    if unresolved:
        warnings.append({'code': 'refinement_incomplete', 'message': 'Для части неясных моментов дополнительный просмотр не завершён в пределах лимита. Проверьте эти места вручную.'})
    if len(unique_steps) > 30:
        warnings.append({'code': 'instruction_step_limit', 'message': 'Получено больше 30 шагов. Все наблюдения сохранены; разделите материал на инструкции или перенесите нужные шаги вручную.'})
    unique_warnings = {}
    for warning in warnings:
        unique_warnings.setdefault(warning['code'], warning)
    result = {'schema_version': '3' if structured else '2', 'language': session.language, 'steps': unique_steps, 'warnings': list(unique_warnings.values()), 'analysis': analysis_data(session)}
    if temporal_pairs:
        result['temporal_coverage'] = {'version': 'v1', 'pairs': temporal_pairs, 'unresolved_pairs': temporal_unresolved}
    if grounding_pin:
        result['frame_grounding'] = {'version': grounding_pin, 'batches': list(grounding_batches.values())}
    if step_formats == {'teaching_v1'}:
        result['step_format'] = 'teaching_v1'
    elif 'teaching_v1' in step_formats:
        # Preserve the original text in mixed historical results. An unknown
        # format is read-only rather than silently rephrasing stored steps.
        result['step_format'] = 'mixed'
    if structured:
        # Keep state/gap evidence even when it cannot become an instruction.
        # Exact duplicates are harmless; never discard conflicting IDs silently.
        for key, values in [('observations', observations), ('gaps', gaps)]:
            unique = {json.dumps(value, sort_keys=True): value for value in values}
            result[key] = sorted(unique.values(), key=lambda value: (value['start_ms'], value['end_ms'], value.get('id', '')))
    return result
