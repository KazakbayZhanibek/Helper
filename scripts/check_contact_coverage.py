"""Offline contact/coverage regression audit. No inference or dispatch option.

Captures or compares database/accounting hashes, saved proposals and JPEGs.
Manual controls test consistency only; they are not repaired model answers.
"""
import argparse
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import compare_real_video as setup
from check_teaching_video import fingerprint_working

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / 'artifacts/test-results'
RUNS = ('recognition-inline-run-v2.json', 'recognition-dense-run-v1.json', 'recognition-grounded-run-v1.json')
SOURCES = (*RUNS, 'recognition-dense-inputs-v2.json', 'inline-recognition-criteria-v2.json',
           'inline-recognition-quality-v2.json', 'inline-recognition-budget-v2.json')
UNCHANGED_CODE = ('backend/config/settings.py', 'backend/ai_budget/models.py',
                  'backend/ai_budget/services.py', 'backend/ai_drafts/codex_provider.py',
                  'backend/visual_drafts/provider.py', 'scripts/check_inline_recognition.py')
SETTINGS = ('AI_BUDGET_ENABLED', 'AI_BUDGET_DEFAULT_CALLS', 'AI_BUDGET_MAX_CALLS',
            'AI_BUDGET_ORG_DAY_CALLS', 'AI_BUDGET_PLATFORM_DAY_CALLS', 'AI_BUDGET_ORG_MONTH_CALLS',
            'AI_BUDGET_DEFAULT_MICROUSD', 'AI_BUDGET_MAX_MICROUSD', 'AI_BUDGET_ORG_DAY_MICROUSD',
            'AI_BUDGET_PLATFORM_DAY_MICROUSD', 'AI_BUDGET_ORG_MONTH_MICROUSD',
            'VISUAL_ACTION_EVIDENCE', 'VISUAL_FRAME_GROUNDING', 'VISUAL_FRAME_GROUNDING_VERSION',
            'VISUAL_SEMANTIC_REVIEW', 'VISUAL_SHORT_ACTION_COVERAGE', 'VIDEO_FRAME_STRATEGY')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def file_sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def table_snapshot(model):
    values = list(model.objects.order_by(model._meta.pk.name).values())
    return {'count': len(values), 'sha256': digest(values)}


def snapshot():
    from django.conf import settings
    from ai_budget.models import AIUsageReservation, BudgetAccount
    from media_pipeline.models import ProcessingJob
    from visual_drafts.models import VisualProposal, VisualSession
    return {
        'tables': {name: table_snapshot(model) for name, model in (
            ('receipts', AIUsageReservation), ('accounts', BudgetAccount), ('jobs', ProcessingJob),
            ('sessions', VisualSession), ('proposals', VisualProposal))},
        'settings': {key: getattr(settings, key) for key in SETTINGS},
        'unchanged_code': {name: file_sha(ROOT / name) for name in UNCHANGED_CODE},
        'frozen_sources': {name: file_sha(ARTIFACTS / name) for name in SOURCES},
    }


def verify_since_trial(current):
    """Anchor the no-spend claim to the completed trial, not this audit start."""
    previous = json.loads((ARTIFACTS / 'inline-recognition-budget-v2.json').read_text())
    run = json.loads((ARTIFACTS / RUNS[0]).read_text())
    assert previous['status'] == 'PASS_BUDGET_AND_PRESERVATION' and previous['used_or_held_new_calls'] == 2
    assert previous['experiment_remaining_call_slots'] == 0
    for name, row in previous['tables'].items():
        assert current['tables'][name] == {'count': row['total'], 'sha256': row['all_rows_sha256']}, name
    assert current['tables']['accounts'] == {'count': len(previous['accounts']), 'sha256': digest(previous['accounts'])}
    assert current['settings'] == previous['settings']
    for name, sha in current['unchanged_code'].items():
        assert sha == run['configuration']['code_sha256'][name], name
    return {'receipts_jobs_sessions_proposals_and_accounts_unchanged': True,
            'budget_settings_and_provider_code_unchanged': True,
            'fixed_experiment_used_calls': 2, 'fixed_experiment_remaining_calls': 0}


def verify_frozen_jpegs():
    bundle = json.loads((ARTIFACTS / 'recognition-dense-inputs-v2.json').read_text())
    result = []
    for case in bundle['inputs']:
        rows = []
        for frame in case['frames']:
            path = Path(frame['file'])
            sha = file_sha(path)
            assert sha == frame['sha256'] and path.stat().st_size == frame['size_bytes']
            rows.append({'timestamp_ms': frame['timestamp_ms'], 'sha256': sha})
        assert len(rows) == 12
        result.append({'case_id': case['case_id'], 'frames': rows})
    return result


def saved_projection_checks():
    from media_pipeline.storage import private_path
    from visual_drafts import provider, services
    from visual_drafts.models import VisualProposal
    results, requests, working_sources = [], [], {}
    latest_frames = latest_session = latest_raw = None
    for filename in RUNS:
        run = json.loads((ARTIFACTS / filename).read_text())
        for case in run['cases']:
            proposal = VisualProposal.objects.select_related('session__instruction', 'session__asset').get(pk=case['proposal_id'])
            selected = proposal.session
            assert proposal.content == case['proposal'] and proposal.applied_at is None
            assert str(proposal.source_job_id) == case['job_id'] and str(selected.id) == case['session_id']
            frames = list(selected.frames.order_by('sequence'))
            expected = {frame['id']: frame for frame in case['sent_frames']}
            assert {str(frame.id) for frame in frames} == set(expected)
            images = []
            for frame in frames:
                frozen = expected[str(frame.id)]
                raw = services.safe_frame_path(frame.storage_key).read_bytes()
                assert hashlib.sha256(raw).hexdigest() == frame.sha256 == frozen['sha256']
                assert frame.timestamp_ms == frozen['timestamp_ms'] and len(raw) == frame.size_bytes
                images.append({'id': str(frame.id), 'sequence': frame.sequence, 'timestamp_ms': frame.timestamp_ms,
                               'sha256': frame.sha256, 'bytes': raw})
            shown = services.proposal_data(proposal)
            projected = services.checked_instruction_content(proposal.content, frames, selected)
            assert services.checked_instruction_content(projected, frames, selected) == projected
            assert projected['steps'] == shown['steps']
            results.append({'source': filename, 'case_id': case['case_id'], 'proposal_id': str(proposal.id),
                            'raw_content_sha256': digest(proposal.content), 'projection_sha256': digest(projected),
                            'steps': len(shown['steps']), 'idempotent': True, 'verified_jpegs': len(frames)})
            working = fingerprint_working(selected.instruction)
            assert working == case['working_before'] == case['working_after']
            working_sources[str(selected.instruction_id)] = {'working': working,
                'asset_id': str(selected.asset_id), 'asset_sha256': selected.asset.sha256,
                'playback_sha256': file_sha(private_path(selected.asset.storage_key))}
            if filename == RUNS[0]:
                engine = provider.VisualProvider(selected.organization)
                engine.protocol, engine.short_action_coverage, engine.frame_grounding = '3', 'v1', 'v2'
                system, schema, request, _ = engine.prepare_request(language=selected.language, mode='upload', frames=images)
                size = len(json.dumps({'system': system, 'schema': schema, 'request': request}, ensure_ascii=False).encode()) + 1024
                assert size <= 32768
                requests.append({'case_id': case['case_id'], 'bytes_with_cli_allowance': size,
                    'input_limit_bytes': 32768, 'output_limit_bytes': 32768, 'image_count': len(images),
                    'system_sha256': hashlib.sha256(system.encode()).hexdigest(), 'schema_sha256': digest(schema)})
                old_size = run['configuration']['request_text_schema_wrapper_bytes'][case['case_id']]
                assert size <= old_size and digest(schema) == run['configuration']['schema_sha256']
                requests[-1].update(previous_trial_bytes=old_size, byte_difference=size - old_size,
                                    unchanged_schema=True)
                latest_frames, latest_session, latest_raw = frames, selected, case['raw_model_response']
    assert [row['steps'] for row in results] == [2, 3, 4, 4, 0, 0]
    return results, requests, working_sources, (latest_frames, latest_session, latest_raw)


def contact_controls(frames, session, raw):
    from visual_drafts import grounding_v2, services
    # Synthetic controls borrow only trusted frame IDs/order. They are not a
    # new interpretation of these JPEGs, nor rewrites of the archived answer.
    action = {'subject': 'Деталь', 'title': 'Переместите деталь', 'movement': 'Переместите деталь рукой.',
              'motion': 'observed', 'evidence': [
                  {'image_index': index, 'subject_visibility': 'visible', 'relations': [
                      {'kind': 'separated_from', 'target': 'Поверхность',
                       'target_visibility': 'visible', 'status': 'observed'}]}
                  for index in (1, 2)]}
    wire = {'schema_version': grounding_v2.WIRE_VERSION, 'language': session.language,
            'actions': [action], 'cut_after_image_indices': []}
    snapshot_raw = deepcopy(raw)
    separation = grounding_v2.normalize_grounded_actions(wire, frames, session)
    assert not separation['steps'] and separation['gaps']
    supported = deepcopy(action)
    for row in supported['evidence']:
        row['relations'] = [
            {'kind': 'held_by', 'target': 'Рука', 'target_visibility': 'visible', 'status': 'observed'},
            {'kind': 'supported_by', 'target': 'Поверхность', 'target_visibility': 'visible', 'status': 'observed'}]
    mixed = grounding_v2.normalize_grounded_actions({**wire, 'actions': [action, supported]}, frames, session)
    assert len(mixed['steps']) == 1 and len(mixed['observations']) == 1
    assert mixed['observations'][0]['movement'] == supported['movement']
    assert services.checked_instruction_content(mixed, frames, session) == mixed
    release = deepcopy(action)
    release.update(title='Отпустите деталь', movement='Отпустите деталь рукой.')
    release['evidence'][0]['relations'] = [{'kind': 'held_by', 'target': 'Рука',
                                           'target_visibility': 'visible', 'status': 'observed'}]
    release['evidence'][1]['relations'][0]['target'] = 'Рука'
    release_result = grounding_v2.normalize_grounded_actions({**wire, 'actions': [release]}, frames, session)
    assert len(release_result['steps']) == 1
    assert services.checked_instruction_content(release_result, frames, session) == release_result
    pickup = deepcopy(release)
    pickup.update(title='Поднимите деталь', movement='Возьмите деталь рукой.')
    pickup['evidence'][0]['relations'], pickup['evidence'][1]['relations'] = (
        pickup['evidence'][1]['relations'], pickup['evidence'][0]['relations'])
    pickup_result = grounding_v2.normalize_grounded_actions({**wire, 'actions': [pickup]}, frames, session)
    assert len(pickup_result['steps']) == 1
    unrelated = deepcopy(release)
    unrelated['evidence'][1]['relations'][0]['target'] = 'Другая поверхность'
    unrelated_result = grounding_v2.normalize_grounded_actions({**wire, 'actions': [unrelated]}, frames, session)
    assert not unrelated_result['steps']
    assert raw == snapshot_raw
    return {'synthetic_fixture_only': True, 'separated_only_steps': len(separation['steps']),
            'valid_held_and_supported_sibling_steps': len(mixed['steps']), 'raw_answer_unchanged': True,
            'brief_release_same_target_steps': len(release_result['steps']),
            'brief_pickup_same_target_steps': len(pickup_result['steps']),
            'unrelated_separation_steps': len(unrelated_result['steps']),
            'idempotent_read_apply_projection': True, 'pixel_truth_not_established': True}


def verify_boundary_preparation(working_sources):
    path = ARTIFACTS / 'refinement-boundary-preflight-v2.json'
    prepared = json.loads(path.read_text())
    original = json.loads((ARTIFACTS / 'recognition-dense-inputs-v2.json').read_text())
    assert prepared['status'] == 'PASS' and prepared['model_calls'] == 0
    assert prepared['recognition_evaluated'] is False and prepared['frame_count'] == 24
    assert prepared['input_bundle_sha256'] == file_sha(ARTIFACTS / 'recognition-dense-inputs-v2.json')
    assert prepared['source_asset_sha256'] == original['asset_sha256']
    assert prepared['source_asset_id'] in {item['asset_id'] for item in working_sources.values()}
    assert all(file_sha(ROOT / name) == sha for name, sha in prepared['code_sha256'].items())
    windows, preserved = [], 0
    point = lambda frame: Fraction(frame['pts']) * Fraction(frame['time_base'])
    for old, new in zip(original['inputs'], prepared['windows']):
        assert old['case_id'] == new['case_id']
        assert len(old['frames']) == len(new['frames']) == 12
        current = {point(frame): frame for frame in new['frames']}
        assert len(current) == 12
        for frame in new['frames']:
            assert file_sha(frame['file']) == frame['sha256']
            assert Path(frame['file']).stat().st_size == frame['size_bytes']
            assert old['start_ms'] <= point(frame) * 1000 < old['end_ms']
            assert frame['timestamp_ms'] == int(point(frame) * 1000)
        old_by_point = {point(frame): frame for frame in old['frames']}
        for required in old['required_frames']:
            assert current[point(required)]['sha256'] == old_by_point[point(required)]['sha256']
            preserved += 1
        times = [frame['timestamp_ms'] for frame in new['frames']]
        assert times == sorted(times) == new['after_timestamps_ms']
        windows.append({'case_id': old['case_id'], 'timestamps_ms': times,
                        'first_timestamp_ms': times[0], 'last_timestamp_ms': times[-1],
                        'added_timestamps_ms': new['added_timestamps_ms'],
                        'removed_unprotected_timestamps_ms': new['removed_unprotected_timestamps_ms']})
    assert preserved == 12 and len(windows) == 2
    gap = windows[1]['first_timestamp_ms'] - windows[0]['last_timestamp_ms']
    assert gap == prepared['boundary_gap_ms']['after'] == 40
    return {'source_report': path.name, 'source_report_sha256': file_sha(path),
            'verified_new_jpegs': 24, 'preserved_parent_jpegs': preserved, 'windows': windows,
            'boundary_gap_ms_before': prepared['boundary_gap_ms']['before'], 'boundary_gap_ms_after': gap,
            'shared_request_pair': False, 'new_recognition_quality_measured': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-baseline', action='store_true')
    parser.add_argument('--baseline', type=Path, default=ARTIFACTS / 'contact-coverage-before-v1.json')
    parser.add_argument('--output', type=Path, default=ARTIFACTS / 'contact-coverage-audit-v1.json')
    args = parser.parse_args()
    setup.initialize_django()
    from django.db import connection, transaction
    from ai_drafts import codex_provider
    from visual_drafts import provider

    report = {'version': 1, 'scope': 'offline_saved_answers_and_manual_contact_controls',
              'model_calls': 0, 'database_writes': 0, 'pixel_reverification': False,
              'limitations': 'Saved false support claims are preserved, not proven or repaired. New prompt recognition quality requires a separate bounded trial.'}
    with transaction.atomic(), patch.object(provider.VisualProvider, 'generate', side_effect=AssertionError('NO_MODEL_CALLS')), \
            patch.object(codex_provider, 'generate', side_effect=AssertionError('NO_MODEL_CALLS')), \
            patch.object(provider, 'json_request', side_effect=AssertionError('NO_MODEL_CALLS')):
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA query_only = ON' if connection.vendor == 'sqlite' else 'SET TRANSACTION READ ONLY')
        try:
            report['before'] = snapshot()
            report['unchanged_since_completed_trial'] = verify_since_trial(report['before'])
            report['frozen_jpegs'] = verify_frozen_jpegs()
            projections, requests, working_sources, control_args = saved_projection_checks()
            report['saved_projections'], report['requests'], report['working_sources'] = projections, requests, working_sources
            if not args.capture_baseline:
                baseline = json.loads(args.baseline.read_text())
                assert report['before'] == baseline['before'], 'PERSISTENT_STATE_OR_BUDGET_CHANGED'
                for key in ('frozen_jpegs', 'saved_projections', 'working_sources'):
                    assert report[key] == baseline[key], key
                assert all(new['schema_sha256'] == old['schema_sha256'] and new['image_count'] == old['image_count']
                           and new['bytes_with_cli_allowance'] <= old['bytes_with_cli_allowance']
                           for new, old in zip(requests, baseline['requests']))
                report['manual_controls'] = contact_controls(*control_args)
                report['boundary_preparation'] = verify_boundary_preparation(working_sources)
                report['baseline_sha256'] = file_sha(args.baseline)
                report['unchanged_vs_baseline'] = True
            report['after'] = snapshot()
            assert report['before'] == report['after']
        finally:
            if connection.vendor == 'sqlite':
                with connection.cursor() as cursor:
                    cursor.execute('PRAGMA query_only = OFF')
    report['status'] = 'BASELINE' if args.capture_baseline else 'PASS'
    destination = args.baseline if args.capture_baseline else args.output
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    print(json.dumps({'status': report['status'], 'model_calls': 0, 'latest_steps': 5, 'legacy_steps': 8,
                      'receipts': report['after']['tables']['receipts']['count'], 'output': str(destination)}))


if __name__ == '__main__':
    main()
