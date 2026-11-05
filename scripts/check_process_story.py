"""Read-only presentation audit on saved videos. No model dispatch option."""
from copy import deepcopy
import json
from unittest.mock import patch

import compare_real_video as setup
from check_contact_coverage import ARTIFACTS, snapshot, verify_frozen_jpegs, digest
from check_teaching_video import fingerprint_working


def main():
    setup.initialize_django()
    from django.db import connection, transaction
    from ai_drafts import codex_provider
    from visual_drafts import provider, services
    from visual_drafts.models import VisualProposal
    from visual_drafts.teaching import present_instruction

    prior = json.loads((ARTIFACTS / 'contact-coverage-audit-v1.json').read_text())
    run = json.loads((ARTIFACTS / 'recognition-inline-run-v2.json').read_text())
    ids = [case['proposal_id'] for case in run['cases']]
    report = {'scope': 'saved_answers_presentation_only', 'model_calls': 0,
              'database_writes': 0, 'new_recognition_test': False, 'proposals': []}
    with transaction.atomic(), patch.object(provider.VisualProvider, 'generate', side_effect=AssertionError('NO_MODEL_CALLS')), \
            patch.object(codex_provider, 'generate', side_effect=AssertionError('NO_MODEL_CALLS')), \
            patch.object(provider, 'json_request', side_effect=AssertionError('NO_MODEL_CALLS')):
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA query_only = ON')
        try:
            report['before'] = snapshot()
            # The user may have analysed a video since the previous report.
            # Record that history; never reset or attribute it to this audit.
            report['since_previous_audit'] = {name: {
                'previous_count': previous['count'],
                'current_count': report['before']['tables'][name]['count'],
                'same_rows': previous == report['before']['tables'][name],
            } for name, previous in prior['after']['tables'].items()}
            assert report['before']['tables']['proposals'] == prior['after']['tables']['proposals']
            assert report['before']['settings'] == prior['after']['settings']
            for path in ('backend/ai_budget/services.py', 'backend/config/settings.py', 'backend/ai_drafts/codex_provider.py'):
                assert report['before']['unchanged_code'][path] == prior['after']['unchanged_code'][path]
            report['frozen_jpegs'] = verify_frozen_jpegs()
            kneading = VisualProposal.objects.filter(
                session__instruction_id='9dbeb4c2-60bd-4421-8a94-b3e9536ce343', session__mode='upload'
            ).order_by('-created_at').first()
            ids.append(str(kneading.id))
            for identity in ids:
                proposal = VisualProposal.objects.select_related('session__instruction').get(pk=identity)
                original = deepcopy(proposal.content)
                working = fingerprint_working(proposal.session.instruction)
                frames = list(proposal.session.frames.all())
                checked = services.checked_instruction_content(original, frames, proposal.session)
                shown = services.proposal_data(proposal)
                assert shown == services.proposal_data(proposal)
                expected = present_instruction(checked)
                assert shown['steps'] == expected['steps'] and len(shown['steps']) == len(checked['steps'])
                assert shown['observations'] == checked['observations']
                for old, new in zip(checked['steps'], shown['steps']):
                    assert {k: v for k, v in old.items() if k != 'description'} == {
                        k: v for k, v in new.items() if k != 'description'}
                proposal.refresh_from_db()
                assert proposal.content == original
                assert fingerprint_working(proposal.session.instruction) == working
                report['proposals'].append({'id': identity, 'steps': len(shown['steps']),
                    'original_content_sha256': digest(original), 'working_unchanged': True,
                    'photos_and_evidence_unchanged': True, 'presentation_format': shown.get('presentation_format'),
                    'explanations': [{'title': step['title'], 'description': step['description']} for step in shown['steps']]})
            report['after'] = snapshot()
            assert report['before'] == report['after']
        finally:
            with connection.cursor() as cursor:
                cursor.execute('PRAGMA query_only = OFF')
    report['status'] = 'PASS'
    output = ARTIFACTS / 'process-story-audit.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
    print(json.dumps({'status': report['status'], 'model_calls': 0,
        'steps_retained': [row['steps'] for row in report['proposals']], 'report': str(output)}))


if __name__ == '__main__':
    main()
