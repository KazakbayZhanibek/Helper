"""Explicit, bounded real-video acceptance run. No automatic retries.

Uses existing uploaded sources and the application's real budget ledger.
At most two short videos, one model call each (two with --pair-review).
Paid: $0.013 per video;
an explicitly selected :free model is restricted to zero-priced routes.
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


def select_model(value):
    from ai_drafts.providers import OPENROUTER_BUDGET_MODEL, OPENROUTER_QWEN_MODEL, is_free_openrouter_model
    if value is None:
        return OPENROUTER_BUDGET_MODEL, 13000
    if value == OPENROUTER_QWEN_MODEL:
        return value, 13000
    if not is_free_openrouter_model(value) or not value.endswith(':free'):
        raise ValueError('Select one explicit OpenRouter :free ID or the approved Qwen model.')
    return value, 0


def validate_receipts(receipts, model, max_cost, *, pair_review=False, review_model=None):
    expected = 2 if pair_review else 1
    if len(receipts) != expected:
        raise RuntimeError(f'Expected {expected} recorded model calls.')
    if pair_review:
        keys = [receipt['call_key'].split(':', 1) for receipt in receipts]
        if (max_cost != 0 or any(len(key) != 2 for key in keys)
                or {key[0] for key in keys} != {'window', 'verify'} or keys[0][1] != keys[1][1]):
            raise RuntimeError('Expected a free analysis and a free verification of the same window.')
    for receipt in receipts:
        expected_model = (review_model or model) if pair_review and receipt['call_key'].startswith('verify:') else model
        if receipt['provider'] != 'openrouter' or receipt['model'] != expected_model:
            raise RuntimeError('Receipt did not match the selected OpenRouter model.')
        if max_cost == 0:
            if (receipt['unit'] != 'calls' or receipt['max_units'] != 1
                    or receipt['usage'].get('cost_microusd') != 0
                    or receipt['usage'].get('resolved_model') != expected_model):
                raise RuntimeError('Free-model receipt does not explicitly confirm this model and zero cost.')
        elif receipt['unit'] != 'usd_micros' or receipt['max_units'] > max_cost:
            raise RuntimeError('Paid-model reservation exceeded the specified cap.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instruction', action='append', required=True)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--model', help='Explicit OpenRouter :free model or approved Qwen model for this process only.')
    parser.add_argument('--review-model', help='Separate explicit OpenRouter :free model for --pair-review.')
    wire = parser.add_mutually_exclusive_group()
    wire.add_argument('--span-wire', action='store_true', help='Test the distinct frame-pair contract.')
    wire.add_argument('--shared-captions', action='store_true', help='Test frame pairs with a shared caption per frame.')
    wire.add_argument('--pair-review', action='store_true', help='Check frame pairs in a second free request.')
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
    try:
        target_model, max_cost = select_model(args.model)
    except ValueError as error:
        parser.error(str(error))
    if args.pair_review and max_cost != 0:
        parser.error('--pair-review requires an explicit :free model.')
    if args.review_model:
        if not args.pair_review:
            parser.error('--review-model requires --pair-review.')
        try:
            _, review_cost = select_model(args.review_model)
        except ValueError as error:
            parser.error(str(error))
        if review_cost != 0:
            parser.error('--review-model requires an explicit :free model.')
    review_model = (args.review_model or target_model) if args.pair_review else None
    calls_per_video = 2 if args.pair_review else 1
    if args.model is not None:
        # A concrete, explicitly approved ID is essential; no dynamic routing.
        settings.STEP_DRAFT_MODEL = args.model
    settings.VISUAL_ACTION_EVIDENCE = settings.VISUAL_SHORT_ACTION_COVERAGE = settings.VISUAL_FRAME_GROUNDING = True
    settings.VISUAL_FRAME_GROUNDING_VERSION = 'v3'
    settings.VISUAL_DEMONSTRATION_SPANS = args.span_wire
    settings.VISUAL_DEMONSTRATION_SHARED_CAPTIONS = args.shared_captions or args.pair_review
    settings.VISUAL_DEMONSTRATION_PAIR_REVIEW = args.pair_review
    settings.VISUAL_DEMONSTRATION_PAIR_REVIEW_MODEL = review_model or ''
    items = []
    for identity in args.instruction:
        instruction = Instruction.objects.select_related('source_asset', 'working_copy', 'organization').get(pk=identity, lifecycle='active')
        if not instruction.source_asset or instruction.source_asset.status != 'ready' or not 0 < instruction.source_asset.duration_ms <= 30000:
            parser.error('Each source must be ready and at most 30 seconds.')
        config = services.capabilities(instruction.organization)
        if not config['available'] or (config['provider'], config['model']) != ('openrouter', target_model):
            parser.error('This bounded experiment requires the selected OpenRouter profile.')
        member = Membership.objects.select_related('user', 'organization').filter(organization=instruction.organization, role='admin', is_active=True, user__is_active=True).first()
        if member is None:
            parser.error('An active administrator is required.')
        items.append((instruction, member))
    experiment = 'demonstrations-v1'
    if args.pair_review:
        experiment = 'demonstration-pair-review-v1'
    elif args.shared_captions:
        experiment = 'demonstration-shared-captions-v3'
    elif args.span_wire:
        experiment = 'demonstration-spans-v2'
    report = {'experiment': experiment,
              'execute': args.execute, 'max_calls': len(items) * calls_per_video,
              'model': target_model, 'max_reserved_microusd': len(items) * max_cost, 'cases': []}
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
            {'consent_images': True, 'scope': 'full', 'max_ai_calls': calls_per_video, 'max_cost_microusd': max_cost}, etag(instruction.working_copy))
        case['queued'] = result;save()
        if status != 202:
            raise RuntimeError('Acceptance request did not queue a fresh job.')
        if not process_one():
            raise RuntimeError('Acceptance worker did not claim a job.')
        job = ProcessingJob.objects.get(id=result['id'], instruction=instruction, kind='analyze_video')
        case.update(job_id=str(job.id), state=job.state, error_code=job.error_code,
                    elapsed_seconds=round(time.monotonic()-started, 3))
        receipts = list(AIUsageReservation.objects.filter(job_id=job.id).values('state', 'unit', 'provider', 'model', 'max_units', 'actual_units', 'usage', 'call_key'))
        case['receipts'] = receipts
        instruction.working_copy.refresh_from_db()
        case['working_unchanged'] = instruction.working_copy.revision == before_revision
        proposal = VisualProposal.objects.filter(source_job_id=job.id).first()
        if proposal:
            case['proposal_id'] = str(proposal.id)
            case['result'] = services.proposal_data(proposal)
        save()
        validate_receipts(receipts, target_model, max_cost, pair_review=args.pair_review, review_model=review_model)
        assert case['working_unchanged']
        print(json.dumps({key: case[key] for key in ('instruction_id', 'state', 'error_code', 'elapsed_seconds')}, ensure_ascii=False), flush=True)
        if (job.state != 'done' or proposal is None
                or not case['result'].get('analysis', {}).get('coverage_complete')
                or (args.pair_review and not services.pair_review_complete(proposal.content, proposal.session))
                or any(receipt['state'] != 'settled' for receipt in receipts)):
            raise RuntimeError('Real acceptance failed; no automatic retry allowed.')


if __name__ == '__main__':
    main()
