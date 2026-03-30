import os
import sys
import subprocess

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Launch Playwright codegen to record automation script (requires local GUI)"

    def add_arguments(self, parser):
        parser.add_argument("--url", required=True)
        parser.add_argument("--target", default="python", choices=["python", "ts", "js"])
        parser.add_argument("--output", default="")

    def handle(self, *args, **options):
        url = str(options.get("url") or "").strip()
        if not url:
            raise CommandError("missing --url")
        target = str(options.get("target") or "python").strip().lower()
        output = str(options.get("output") or "").strip()
        if not output:
            ext = "py" if target == "python" else "ts"
            output = os.path.join("recordings", f"playwright_codegen.{ext}")

        out_dir = os.path.dirname(output)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        cmd = [sys.executable, "-m", "playwright", "codegen", url, "--target", target, "-o", output]
        self.stdout.write(" ".join([str(x) for x in cmd]))
        try:
            subprocess.check_call(cmd)
        except Exception as e:
            raise CommandError(str(e))

