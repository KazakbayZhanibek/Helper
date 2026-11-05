"""Offline projection of archived responses. No dispatch mode or API calls.

Does not invent a frame-fact ledger for old answers. Tests only the applicable
legacy safeguards; new ledger recognition quality requires a separate trial.
"""
import argparse
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import compare_real_video as setup
from check_teaching_video import fingerprint_working

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('short-actions-run-v1.json', 'recognition-dense-run-v1.json')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/test-results/instruction-grounding-replay-v1.json')
    args = parser.parse_args()
    setup.initialize_django()
    from django.db import connection, transaction
    from ai_budget.models import AIUsageReservation
    from ai_drafts import codex_provider
    from visual_drafts import provider, services
    from visual_drafts.models import VisualProposal, VisualSession
    from media_pipeline.models import ProcessingJob

    def snapshot():
        return {
            'receipts': AIUsageReservation.objects.count(),
            'receipts_sha256': digest(list(AIUsageReservation.objects.order_by('id').values())),
            'jobs': ProcessingJob.objects.count(),
            'sessions': VisualSession.objects.count(),
            'proposals': VisualProposal.objects.count(),
        }

    report = {'version': 1, 'scope': 'offline_saved_answer_projection_only', 'model_calls': 0,
              'database_writes': 0, 'new_frame_fact_ledger_generated': False, 'sources': {}, 'cases': [],
              'limitation': 'Legacy frames were not reinterpreted. Remaining false phase descriptions are not repaired or declared correct.'}
    with transaction.atomic(), patch.object(provider.VisualProvider, 'generate', side_effect=AssertionError('NO_MODEL_CALLS')), \
            patch.object(codex_provider, 'generate', side_effect=AssertionError('NO_MODEL_CALLS')):
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA query_only = ON' if connection.vendor == 'sqlite' else 'SET TRANSACTION READ ONLY')
        try:
            report['database_before'] = snapshot()
            for filename in SOURCES:
                path = ROOT / 'artifacts/test-results' / filename
                raw_bytes = path.read_bytes()
                report['sources'][filename] = hashlib.sha256(raw_bytes).hexdigest()
                source = json.loads(raw_bytes)
                for case in source['cases']:
                    if case['status'] != 'done':
                        continue
                    proposal = VisualProposal.objects.select_related('session__instruction__working_copy', 'session__asset').get(pk=case['proposal_id'])
                    selected = proposal.session
                    assert str(proposal.source_job_id) == case['job_id'] and str(selected.id) == case['session_id']
                    assert proposal.applied_at is None and proposal.content == case['proposal']
                    working_before = fingerprint_working(selected.instruction)
                    frames = list(selected.frames.order_by('sequence'))
                    expected = {f['id']: f for f in case['sent_frames']}
                    assert {str(f.id) for f in frames} == set(expected)
                    for frame in frames:
                        ref = expected[str(frame.id)]
                        assert frame.timestamp_ms == ref['timestamp_ms'] and frame.sha256 == ref['sha256']
                        data = services.safe_frame_path(frame.storage_key).read_bytes()
                        assert hashlib.sha256(data).hexdigest() == frame.sha256 and len(data) == frame.size_bytes
                    raw = case['raw_model_response']
                    options = {'expected_version': '3'}
                    if raw['schema_version'] == '3-short-actions-v1':
                        options['expected_coverage'] = 'v1'
                    normalized = provider.validate(raw, frames, selected, **options)
                    shown = services.proposal_data(proposal)
                    # Fresh normalization and stored read projection must agree.
                    for key in ('steps', 'observations'):
                        assert normalized[key] == shown[key], (case['case_id'], key)
                    # Historical transition questions used a different stable
                    # ID and aggregate sorting. Compare their actual content.
                    def questions(value):
                        return sorted(digest({k: v for k, v in gap.items() if k != 'id'}) for gap in value['gaps'])
                    assert questions(normalized) == questions(shown), (case['case_id'], 'gap semantics')
                    projected = services.checked_instruction_content(proposal.content, frames, selected)
                    assert services.checked_instruction_content(projected, frames, selected) == projected
                    kept = {o['id'] for o in shown['observations']}
                    removed = [o for o in proposal.content.get('observations', []) if o['id'] not in kept]
                    report['cases'].append({
                        'source': filename, 'case_id': case['case_id'], 'proposal_id': case['proposal_id'],
                        'source_proposal_sha256': digest(proposal.content), 'verified_jpegs': len(frames),
                        'stored_step_count': len(proposal.content['steps']), 'projected_step_count': len(shown['steps']),
                        'removed_commands': [{'id': o['id'], 'title': o['title'], 'movement': o['movement']} for o in removed],
                        'phase_texts_not_rewritten': all(
                            o['before'] == next(p for p in proposal.content['observations'] if p['id'] == o['id'])['before']
                            and o['after'] == next(p for p in proposal.content['observations'] if p['id'] == o['id'])['after']
                            for o in shown['observations']),
                        'neutral_questions': shown['gaps'], 'working_before': working_before,
                        'working_after': fingerprint_working(selected.instruction),
                        'fresh_semantics_equal_read_projection': True, 'idempotent': True,
                    })
                    assert report['cases'][-1]['working_before'] == report['cases'][-1]['working_after']
                    proposal.refresh_from_db()
                    assert proposal.content == case['proposal'] and proposal.applied_at is None
            report['database_after'] = snapshot()
            assert report['database_before'] == report['database_after']
        finally:
            if connection.vendor == 'sqlite':
                with connection.cursor() as cursor:
                    cursor.execute('PRAGMA query_only = OFF')
    report['status'] = 'PASS'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': report['status'], 'model_calls': 0, 'cases': len(report['cases']), 'output': str(args.output)}))


if __name__ == '__main__':
    main()
