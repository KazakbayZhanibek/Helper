import logging
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from customer_accounts.delivery import deliver_one

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Deliver account email independently of video processing.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        while True:
            try:
                close_old_connections()
                delivered = deliver_one()
            except Exception:
                # No message body, SMTP credentials or bearer URLs in worker logs.
                logger.error('Account email worker could not process the queue.')
                if options['once']:
                    raise CommandError('Account email queue unavailable.') from None
                delivered = False
            if options['once']:
                return
            if not delivered:
                time.sleep(2)
