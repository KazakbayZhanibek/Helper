from django.core.management.base import BaseCommand
from visual_drafts.cleanup import cleanup_visual_sessions


class Command(BaseCommand):
    help = 'Expire camera sessions and remove eligible unused private frames.'

    def handle(self, *args, **options):
        result = cleanup_visual_sessions()
        self.stdout.write(f"Expired: {result['expired']}; purged: {result['purged']}")
