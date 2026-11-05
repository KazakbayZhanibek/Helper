"""Untrusted provider output is data, never an operation or a working copy."""
import copy
import json
import re
from decimal import Decimal
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA = json.loads((Path(__file__).resolve().parents[2] / 'contracts' / 'step-draft.schema.json').read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
MAX_OUTPUT_BYTES = 262_144


class InvalidModelOutput(ValueError):
    pass


def parse_json(raw):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_OUTPUT_BYTES:
        raise InvalidModelOutput('MODEL_OUTPUT_INVALID')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidModelOutput('MODEL_OUTPUT_INVALID')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(InvalidModelOutput('MODEL_OUTPUT_INVALID')))
    except (ValueError, RecursionError, TypeError) as error:
        raise InvalidModelOutput('MODEL_OUTPUT_INVALID') from error


def numeric_values(text):
    return {Decimal(value.replace(',', '.')) for value in re.findall(r'(?<!\w)[+-]?\d+(?:[.,]\d+)?', text)}


UNIT_PATTERN = re.compile(r'(?<!\w)(?:°\s*[cсfф]|мм|см|км|мл|кг|мг|г|м|л|вт|квт|бар|psi|rpm|°с|секунд\w*|минут\w*|час\w*|mm|cm|kg|ml|minutes?|seconds?)(?!\w)', re.I)


def validate_proposal(value, segments, *, language, duration_ms):
    """Return a new validated object. All checks use one immutable transcript."""
    try:
        errors = next(VALIDATOR.iter_errors(value), None)
    except (TypeError, ValueError, RecursionError) as error:
        raise InvalidModelOutput('MODEL_OUTPUT_INVALID') from error
    if errors is not None or value['language'] != language:
        raise InvalidModelOutput('MODEL_OUTPUT_INVALID')
    by_id = {str(segment['id']): segment for segment in segments}
    validated = copy.deepcopy(value)
    previous_ranges = []
    overlaps = False
    for step in validated['steps']:
        refs = step['evidence_segment_ids']
        if any(ref not in by_id for ref in refs):
            raise InvalidModelOutput('MODEL_EVIDENCE_INVALID')
        selected = [by_id[ref] for ref in refs]
        ordinals = [segment['ordinal'] for segment in selected]
        if ordinals != list(range(min(ordinals), max(ordinals) + 1)):
            raise InvalidModelOutput('MODEL_EVIDENCE_INVALID')
        expected_start = min(segment['start_ms'] for segment in selected)
        expected_end = max(segment['end_ms'] for segment in selected)
        # JSON Schema integers accept 1.0; the API deliberately requires ints.
        if type(step['start_ms']) is not int or type(step['end_ms']) is not int or step['start_ms'] != expected_start or step['end_ms'] != expected_end or not 0 <= expected_start < expected_end <= duration_ms:
            raise InvalidModelOutput('MODEL_TIMING_INVALID')
        source = ' '.join(segment['text'] for segment in selected)
        generated = step['title'] + ' ' + step['description']
        if not numeric_values(generated).issubset(numeric_values(source)):
            raise InvalidModelOutput('MODEL_UNSUPPORTED_PARAMETERS')
        source_units = {unit.lower().replace(' ', '') for unit in UNIT_PATTERN.findall(source)}
        generated_units = {unit.lower().replace(' ', '') for unit in UNIT_PATTERN.findall(generated)}
        if not generated_units.issubset(source_units):
            raise InvalidModelOutput('MODEL_UNSUPPORTED_PARAMETERS')
        if not step['title'].strip() or not step['description'].strip():
            raise InvalidModelOutput('MODEL_OUTPUT_INVALID')
        if any(expected_start < end and expected_end > start for start, end in previous_ranges):
            overlaps = True
        previous_ranges.append((expected_start, expected_end))
    if overlaps and not any(w['code'] == 'overlapping_ranges' for w in validated['warnings']):
        if len(validated['warnings']) == 30:
            validated['warnings'].pop()
        validated['warnings'].append({'code': 'overlapping_ranges', 'message': 'Временные интервалы шагов пересекаются. Проверьте порядок по видео.'})
    if not validated['steps'] and not any(w['code'] in {'insufficient_evidence', 'non_procedural_speech'} for w in validated['warnings']):
        if len(validated['warnings']) == 30:
            validated['warnings'].pop()
        validated['warnings'].append({'code': 'insufficient_evidence', 'message': 'В речи недостаточно сведений для последовательности действий. Добавьте шаги вручную.'})
    return validated
