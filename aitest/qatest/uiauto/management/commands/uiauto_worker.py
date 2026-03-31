import time

from django.core.management.base import BaseCommand

from uiauto.services.execution_queue import start_worker


class Command(BaseCommand):
    help = "Run UI automation worker"

    def add_arguments(self, parser):
        parser.add_argument("--workers", type=int, default=1)

    def handle(self, *args, **options):
        workers = int(options.get("workers") or 1)
        start_worker(workers=workers, daemon=False)
        self.stdout.write(self.style.SUCCESS(f"UIAuto worker started with {workers} worker(s)."))
        while True:
            time.sleep(3600)

