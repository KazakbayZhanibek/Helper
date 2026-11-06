"""Remove old AI proposals from the UI without erasing costs or source media."""
import json
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from core.models import Organization
from ai_drafts.models import DraftProposal
from visual_drafts.models import VisualProposal
from media_pipeline.models import ProcessingJob


class Command(BaseCommand):
    help = 'Archive existing AI proposals for one organization; preserves instructions, media and the budget ledger.'

    def add_arguments(self, parser):
        parser.add_argument('--organization', required=True)
        parser.add_argument('--execute', action='store_true', help='Apply the archive; otherwise show counts only.')

    @transaction.atomic
    def handle(self, *args, **options):
        try:
            organization = Organization.objects.select_for_update().get(pk=options['organization'])
        except (Organization.DoesNotExist, ValueError):
            raise CommandError('Organization not found.') from None
        cutoff = timezone.now()
        result = {'organization': str(organization.id), 'executed': options['execute'], 'before': cutoff.isoformat()}
        for name, model in [('visual_proposals', VisualProposal), ('text_proposals', DraftProposal)]:
            query = model.objects.filter(organization=organization, archived_at__isnull=True, created_at__lte=cutoff)
            result[name] = query.update(archived_at=cutoff) if options['execute'] else query.count()
        jobs = ProcessingJob.objects.filter(organization=organization, archived_at__isnull=True,
                created_at__lte=cutoff, state__in=['done', 'failed', 'cancelled']).exclude(kind='prepare_media')
        result['finished_ai_jobs'] = jobs.update(archived_at=cutoff) if options['execute'] else jobs.count()
        self.stdout.write(json.dumps(result))
