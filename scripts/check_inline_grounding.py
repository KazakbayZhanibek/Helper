"""Read-only v2 contract preflight with real JPEGs and explicitly manual fixtures.

No dispatch option. Saved model answers are never converted or repaired into v2.
This checks transport, filtering and history compatibility, not new recognition.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import compare_real_video as setup
from check_teaching_video import fingerprint_working

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / 'artifacts/test-results'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ARTIFACTS / 'inline-grounding-preflight-v2.json')
    args = parser.parse_args()
    setup.initialize_django()
    from django.db import connection, transaction
    from ai_budget.models import AIUsageReservation
    from ai_drafts import codex_provider
    from ai_drafts.validation import InvalidModelOutput
    from media_pipeline.models import ProcessingJob
    from visual_drafts import grounding_v2, provider, services
    from visual_drafts.models import VisualProposal, VisualSession

    def snapshot():
        return {
            'receipts': AIUsageReservation.objects.count(),
            'receipts_sha256': digest(list(AIUsageReservation.objects.order_by('id').values())),
            'jobs': ProcessingJob.objects.count(),
            'jobs_sha256': digest(list(ProcessingJob.objects.order_by('id').values())),
            'sessions': VisualSession.objects.count(),
            'proposals': VisualProposal.objects.count(),
            'proposals_sha256': digest(list(VisualProposal.objects.order_by('id').values())),
        }

    report = {'version': 2, 'scope': 'offline_contract_preflight_with_manual_fixtures',
              'new_model_calls': 0, 'database_writes': 0, 'paid_api_calls': 0,
              'fixture_origin': 'Hand-authored local controls; not generated recognition and not repaired saved answers.',
              'cases': [], 'legacy_projection_checks': [], 'sources': {}}
    paths = {name: ARTIFACTS / name for name in (
        'recognition-grounded-run-v1.json', 'grounded-recognition-baseline-v1.json',
        'recognition-dense-inputs-v2.json')}
    data = {}
    for name, path in paths.items():
        raw = path.read_bytes()
        data[name] = json.loads(raw)
        report['sources'][name] = hashlib.sha256(raw).hexdigest()

    with transaction.atomic(), patch.object(provider.VisualProvider, 'generate', side_effect=AssertionError('NO_MODEL_CALLS')), \
            patch.object(codex_provider, 'generate', side_effect=AssertionError('NO_MODEL_CALLS')), \
            patch.object(provider, 'json_request', side_effect=AssertionError('NO_MODEL_CALLS')):
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA query_only = ON' if connection.vendor == 'sqlite' else 'SET TRANSACTION READ ONLY')
        try:
            report['database_before'] = snapshot()
            for frozen in data['grounded-recognition-baseline-v1.json']['cases']:
                saved = VisualProposal.objects.select_related('session__instruction', 'session__asset').get(pk=frozen['proposal_id'])
                shown = services.proposal_data(saved)
                assert digest(shown) == frozen['current_projection_sha256']
                report['legacy_projection_checks'].append({'proposal_id': str(saved.id),
                    'unchanged': True, 'steps': len(shown['steps'])})

            for case in data['recognition-grounded-run-v1.json']['cases']:
                saved = VisualProposal.objects.select_related('session__instruction', 'session__asset').get(pk=case['proposal_id'])
                old_session = saved.session
                frames = list(old_session.frames.order_by('timestamp_ms'))
                expected = {frame['id']: frame for frame in case['sent_frames']}
                images = []
                for frame in frames:
                    raw = services.safe_frame_path(frame.storage_key).read_bytes()
                    assert hashlib.sha256(raw).hexdigest() == expected[str(frame.id)]['sha256'] == frame.sha256
                    assert len(raw) == frame.size_bytes
                    images.append({'id': str(frame.id), 'sequence': frame.sequence,
                                   'timestamp_ms': frame.timestamp_ms, 'sha256': frame.sha256, 'bytes': raw})
                working_before = fingerprint_working(old_session.instruction)
                selected = provider.VisualProvider(old_session.organization)
                selected.protocol, selected.short_action_coverage, selected.frame_grounding = '3', 'v1', 'v2'
                system, schema, request, _ = selected.prepare_request(language='ru', mode='upload', frames=images)
                size = len(json.dumps({'system': system, 'schema': schema, 'request': request}, ensure_ascii=False).encode()) + 1024
                assert size <= 32768 and len(images) == 12
                assert [row['image_index'] for row in request['frames']] == list(range(1, 13))
                assert 'temporal_pairs' not in request
                assert all('id' not in row for row in request['frames'])
                ephemeral = SimpleNamespace(mode='upload', language='ru', asset=old_session.asset,
                    window_start_ms=old_session.window_start_ms, window_end_ms=old_session.window_end_ms,
                    analysis_budget={'visual_protocol': '3', 'short_action_coverage': 'v1', 'frame_grounding': 'v2'})
                # Do not reinterpret the archived v1 ledger as new evidence.
                try:
                    grounding_v2.normalize_grounded_actions(case['raw_model_response'], frames, ephemeral)
                except InvalidModelOutput:
                    pass
                else:
                    raise AssertionError('V1_ANSWER_MUST_NOT_BECOME_V2')
                result = {'case_id': case['case_id'], 'verified_jpegs': len(images),
                          'input_bytes_including_cli_allowance': size, 'input_limit_bytes': 32768,
                          'output_limit_bytes': 32768, 'frames_limit': 12,
                          'prompt_sha256': hashlib.sha256(system.encode()).hexdigest(),
                          'schema_sha256': digest(schema), 'old_answer_not_reinterpreted': True}
                if case['case_id'].endswith('anchored-1'):
                    # Actual images 4/5 are used only to check links and photo roles.
                    # Truth of this manually written control is not an AI result.
                    holding = {'kind': 'held_by', 'target': 'Рука', 'target_visibility': 'visible', 'status': 'observed'}
                    action = {'subject': 'Зелёный овощ', 'title': 'Опустите нож поперёк овоща',
                              'movement': 'Придерживайте овощ рукой и опускайте нож поперёк него.',
                              'motion': 'observed', 'evidence': [
                                  {'image_index': 4, 'subject_visibility': 'visible', 'relations': [deepcopy(holding)]},
                                  {'image_index': 5, 'subject_visibility': 'visible', 'relations': [deepcopy(holding),
                                      {'kind': 'touching', 'target': 'Нож', 'target_visibility': 'visible', 'status': 'observed'}]},
                              ]}
                    wire = {'schema_version': grounding_v2.WIRE_VERSION, 'language': 'ru',
                            'actions': [action], 'cut_after_image_indices': []}
                    good = grounding_v2.normalize_grounded_actions(wire, frames, ephemeral)
                    assert len(good['steps']) == 1
                    assert good['steps'][0]['evidence_frame_ids'] == [str(frame.id) for frame in frames[3:5]]
                    assert services.checked_instruction_content(good, frames, ephemeral) == good
                    questionable = deepcopy(action)
                    questionable.update(title='Выполните неподтверждённую операцию', movement='Положите деталь и затяните крепёж.')
                    questionable['evidence'][1]['relations'].append(
                        {'kind': 'touching', 'target': 'Инструмент', 'target_visibility': 'unclear', 'status': 'unclear'})
                    wire['actions'].append(questionable)
                    mixed = grounding_v2.normalize_grounded_actions(wire, frames, ephemeral)
                    assert len(mixed['steps']) == 1 and mixed['steps'][0]['title'] == action['title']
                    assert all(item['title'] != questionable['title'] for item in mixed['observations'])
                    assert all(questionable['movement'] not in gap['description'] for gap in mixed['gaps'])
                    assert services.checked_instruction_content(mixed, frames, ephemeral) == mixed
                    result['manual_controls'] = {'supported_card_steps': 1, 'mixed_cards_steps': 1,
                        'unconfirmed_compound_card_removed': True, 'valid_sibling_preserved': True,
                        'idempotent_projection': True, 'exact_photo_roles': True}
                    # These intentionally invented details are local controls,
                    # not a new model answer or a pixel-level quality measure.
                    prose_controls = []
                    for label, detail in (
                        ('unmeasured_force', ' Приложите усилие 20 Н.'),
                        ('unprovided_sound', ' Дождитесь щелчка.'),
                        ('categorical_guarantee', ' Этот способ полностью безопасен.'),
                    ):
                        invented = deepcopy(action)
                        invented['movement'] += detail
                        prose_wire = {**wire, 'actions': [deepcopy(action), invented]}
                        filtered = grounding_v2.normalize_grounded_actions(prose_wire, frames, ephemeral)
                        assert len(filtered['steps']) == 1
                        assert filtered['observations'][0]['movement'] == action['movement']
                        assert filtered['steps'][0]['evidence_frame_ids'] == good['steps'][0]['evidence_frame_ids']
                        assert all(invented['movement'] not in gap['description'] for gap in filtered['gaps'])
                        assert services.checked_instruction_content(filtered, frames, ephemeral) == filtered
                        prose_controls.append({'case': label, 'kept_steps': 1,
                            'unsupported_card_removed': True, 'supported_sibling_and_photos_preserved': True})
                    result['manual_prose_controls'] = prose_controls
                assert fingerprint_working(old_session.instruction) == working_before
                assert saved.content == case['proposal'] and saved.applied_at is None
                result['working_before'] = result['working_after'] = working_before
                report['cases'].append(result)
            report['database_after'] = snapshot()
            assert report['database_before'] == report['database_after']
        finally:
            if connection.vendor == 'sqlite':
                with connection.cursor() as cursor:
                    cursor.execute('PRAGMA query_only = OFF')
    report['status'] = 'PASS'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    print(json.dumps({'status': report['status'], 'new_model_calls': 0,
                      'cases': len(report['cases']), 'output': str(args.output)}))


if __name__ == '__main__':
    main()
