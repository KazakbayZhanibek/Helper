import time
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from media_pipeline.worker import process_one
from media_pipeline.maintenance import maintenance_tick

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Run the durable media/AI queue sequentially. Use one process with SQLite.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Process at most one queued job and exit.')
        parser.add_argument('--poll-seconds', type=float, default=2.0)

    def handle(self, *args, **options):
        self.stdout.write('Media/AI worker ready (sequential durable queue).')
        next_maintenance = 0.0
        try:
            while True:
                failed = False
                try:
                    close_old_connections()
                    if time.monotonic() >= next_maintenance:
                        # Even a failed maintenance pass waits before trying
                        # again; it must not starve queued work on every poll.
                        next_maintenance = time.monotonic() + 60
                        try:
                            maintenance_tick()
                        except Exception:
                            logger.error('Worker maintenance failed; retrying on next scheduled pass.')
                            failed = True
                    processed = process_one()
                except Exception:
                    # Do not log exception text: it may contain source data or
                    # credentials. Leased jobs recover through existing fencing.
                    logger.error('Worker iteration failed; queue will retry after a pause.')
                    failed, processed = True, False
                if options['once']:
                    if failed:
                        raise CommandError('Worker iteration failed; inspect worker status before retrying.')
                    return
                if failed or not processed:
                    time.sleep(max(1.0 if failed else 0.2, min(options['poll_seconds'], 30)))
        except KeyboardInterrupt:
            self.stdout.write('Worker stopped; uncommitted work will recover after its lease expires.')
