"""Explicit, bounded real-video acceptance run. No automatic retries.

Uses existing uploaded sources and the application's real budget ledger.
At most two short videos, one model call each, and $0.026 reserved in total.
The report must not already exist; this script never applies model suggestions.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from dev import load_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instruction', action='append', required=True)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not 1 <= len(set(args.instruction)) == len(args.instruction) <= 2:
        parser.error('Choose one or two distinct existing instructions.')
    if args.report.exists():
        parser.error('Report already exists; an experiment cannot be replayed.')
    os.environ.update(load_environment())
    sys.path.insert(0, str(ROOT / 'backend'))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from django.conf import settings
    from core.models import Instruction, Membership
    from core.services import etag
    from visual_drafts import services
    from visual_drafts.models import VisualProposal
    from media_pipeline.models import ProcessingJob
    from media_pipeline.worker import process_one
    from ai_budget.models import AIUsageReservation
    settings.VISUAL_ACTION_EVIDENCE = settings.VISUAL_SHORT_ACTION_COVERAGE = settings.VISUAL_FRAME_GROUNDING = True
    settings.VISUAL_FRAME_GROUNDING_VERSION = 'v3'
    items = []
    for identity in args.instruction:
        instruction = Instruction.objects.select_related('source_asset', 'working_copy', 'organization').get(pk=identity, lifecycle='active')
        if not instruction.source_asset or instruction.source_asset.status != 'ready' or not 0 < instruction.source_asset.duration_ms <= 30000:
            parser.error('Each source must be ready and at most 30 seconds.')
        config = services.capabilities(instruction.organization)
        if not config['available'] or (config['provider'], config['model']) != ('openrouter', 'google/gemini-2.5-flash-lite'):
            parser.error('This bounded experiment requires the existing Flash-Lite profile.')
        member = Membership.objects.select_related('user', 'organization').filter(organization=instruction.organization, role='admin', is_active=True, user__is_active=True).first()
        if member is None:
            parser.error('An active administrator is required.')
        items.append((instruction, member))
    report = {'experiment': 'demonstrations-v1', 'execute': args.execute, 'max_calls': len(items),
              'max_reserved_microusd': len(items) * 13000, 'cases': []}
    if not args.execute:
        print(json.dumps(report));return
    if ProcessingJob.objects.filter(state__in=['queued', 'running']).exists():
        parser.error('Stop other processing before this isolated acceptance run.')
    # This metadata request does not run a model. Reject a revoked key before
    # creating jobs/reservations, instead of producing empty "done" proposals.
    from ai_drafts.providers import json_request, provider_api_key, ProviderFailure
    try:
        json_request('https://openrouter.ai/api/v1/key', api_key=provider_api_key('openrouter'), timeout=20)
    except ProviderFailure as error:
        parser.error('Provider authentication check failed: ' + error.code)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x') as stream:
        json.dump(report, stream)
    args.report.chmod(0o600)
    def save():
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    for instruction, member in items:
        started = time.monotonic()
        before_revision = instruction.working_copy.revision
        case = {'instruction_id': str(instruction.id), 'before_revision': before_revision}
        report['cases'].append(case);save()
        result, status = services.request_uploaded(member, instruction.id,
            {'consent_images': True, 'scope': 'full', 'max_ai_calls': 1, 'max_cost_microusd': 13000}, etag(instruction.working_copy))
        case['queued'] = result;save()
        if not process_one():
            raise RuntimeError('Acceptance worker did not claim a job.')
        # Scope by the selected instruction and fresh session returned by request.
        job = ProcessingJob.objects.filter(instruction=instruction, kind='analyze_video').latest('created_at')
        case.update(job_id=str(job.id), state=job.state, error_code=job.error_code,
                    elapsed_seconds=round(time.monotonic()-started, 3))
        receipts = list(AIUsageReservation.objects.filter(job_id=job.id).values('state', 'max_units', 'actual_units', 'usage'))
        case['receipts'] = receipts
        instruction.working_copy.refresh_from_db()
        case['working_unchanged'] = instruction.working_copy.revision == before_revision
        proposal = VisualProposal.objects.filter(source_job_id=job.id).first()
        if proposal:
            case['proposal_id'] = str(proposal.id)
            case['result'] = services.proposal_data(proposal)
        save()
        assert len(receipts) <= 1 and sum(r['max_units'] for r in receipts) <= 13000
        assert case['working_unchanged']
        print(json.dumps({key: case[key] for key in ('instruction_id', 'state', 'error_code', 'elapsed_seconds')}, ensure_ascii=False), flush=True)
        if (job.state != 'done' or proposal is None
                or not case['result'].get('analysis', {}).get('coverage_complete')
                or any(receipt['state'] != 'settled' for receipt in receipts)):
            raise RuntimeError('Real acceptance failed; no automatic retry allowed.')


if __name__ == '__main__':
    main()
