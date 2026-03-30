import time

from django.core.management.base import BaseCommand

from autotest.services.execution_queue import start_worker


class Command(BaseCommand):
    help = "Run autotest execution workers"

    def add_arguments(self, parser):
        parser.add_argument("--workers", type=int, default=2)

    def handle(self, *args, **options):
        workers = int(options.get("workers") or 2)
        if workers < 1:
            workers = 1
        start_worker(workers=workers, daemon=False)
        self.stdout.write(self.style.SUCCESS(f"autotest workers started: {workers}"))
        while True:
            time.sleep(60)

