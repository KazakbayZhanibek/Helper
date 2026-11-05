import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from media_pipeline.worker import process_one
from media_pipeline.maintenance import maintenance_tick


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
                close_old_connections()
                if time.monotonic() >= next_maintenance:
                    maintenance_tick()
                    next_maintenance = time.monotonic() + 60
                processed = process_one()
                if options['once']:
                    return
                if not processed:
                    time.sleep(max(0.2, min(options['poll_seconds'], 30)))
        except KeyboardInterrupt:
            self.stdout.write('Worker stopped; uncommitted work will recover after its lease expires.')
