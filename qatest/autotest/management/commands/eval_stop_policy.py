from django.core.management.base import BaseCommand

from autotest.models import AutoTestExecution, AutoTestStepRecord


class Command(BaseCommand):
    help = "Inspect stop decisions and evidence in autotest executions"

    def add_arguments(self, parser):
        parser.add_argument("--execution", type=int, default=0)
        parser.add_argument("--latest", type=int, default=0)

    def handle(self, *args, **options):
        execution_id = int(options.get("execution") or 0)
        latest = int(options.get("latest") or 0)

        qs = AutoTestExecution.objects.select_related("case").order_by("-id")
        if execution_id > 0:
            qs = qs.filter(id=execution_id)
        elif latest > 0:
            qs = qs[:latest]
        else:
            self.stdout.write("Provide --execution <id> or --latest <n>")
            return

        for exe in qs:
            summary = exe.result_summary or {}
            stop_reason = summary.get("stop_reason") or ""
            final_status = summary.get("final_status") or exe.status
            self.stdout.write(self.style.MIGRATE_HEADING(f"Execution {exe.id} | {exe.case.title} | {final_status} | stop_reason={stop_reason}"))

            steps = list(AutoTestStepRecord.objects.filter(execution=exe).order_by("step_number"))
            stop_step = None
            for s in reversed(steps):
                m = s.metrics or {}
                if (m or {}).get("stopped_reason") or (m or {}).get("bug_id"):
                    stop_step = s
                    break
            if not stop_step:
                self.stdout.write("  no stop step metrics found")
                continue

            m = stop_step.metrics or {}
            bug_id = int((m or {}).get("bug_id") or 0)
            sr = (m or {}).get("stopped_reason") or ""
            evidence = (m or {}).get("stop_evidence") or []
            toasts = (m or {}).get("stop_toasts") or []
            nets = (m or {}).get("stop_network") or []
            self.stdout.write(f"  stop_step={stop_step.step_number} status={stop_step.status} stopped_reason={sr} bug_id={bug_id}")
            self.stdout.write(f"  evidence_items={len(evidence)} toasts={len(toasts)} network={len(nets)}")
            if toasts:
                self.stdout.write("  toast_tail=" + " | ".join([str(x) for x in toasts][-5:])[:800])
            if nets:
                self.stdout.write("  network_tail=" + " | ".join([str(x) for x in nets][-5:])[:800])

