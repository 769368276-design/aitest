import time
import datetime
import uuid

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.db import models

from autotest.models import AutoTestSchedule, AutoTestExecution
from autotest.services.execution_queue import enqueue_execution
from autotest.services.datasets import expand_dataset
from testcases.models import TestCase
from core.visibility import visible_projects


class Command(BaseCommand):
    help = "Run autotest schedule dispatcher"

    def add_arguments(self, parser):
        parser.add_argument("--poll", type=int, default=5)
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        poll = int(options.get("poll") or 5)
        if poll < 1:
            poll = 1
        once = bool(options.get("once"))
        limit = int(options.get("limit") or 20)
        if limit < 1:
            limit = 1

        self.stdout.write(self.style.SUCCESS(f"autotest scheduler started (poll={poll}s, limit={limit}, once={once})"))
        while True:
            self._tick(limit=limit)
            if once:
                return
            time.sleep(poll)

    def _tick(self, limit: int = 20) -> None:
        now = timezone.now()
        due = (
            AutoTestSchedule.objects.filter(enabled=True, next_run_at__isnull=False, next_run_at__lte=now)
            .filter(models.Q(locked_until__isnull=True) | models.Q(locked_until__lt=now))
            .order_by("next_run_at", "id")[:limit]
        )
        for s in due:
            try:
                self._run_one(int(s.id), now)
            except Exception:
                continue

    def _run_one(self, schedule_id: int, now: datetime.datetime) -> None:
        with transaction.atomic():
            s = AutoTestSchedule.objects.select_for_update().get(id=schedule_id)
            if not s.enabled:
                return
            if not s.next_run_at or s.next_run_at > now:
                return
            if s.locked_until and s.locked_until > now:
                return
            s.locked_until = now + datetime.timedelta(minutes=5)
            s.save(update_fields=["locked_until"])

        try:
            case_ids = s.case_ids if isinstance(s.case_ids, list) else []
            case_ids = [int(x) for x in case_ids if str(x).strip().isdigit()]
            if not case_ids:
                s.last_status = "no_cases"
                s.last_error = "empty case_ids"
                if s.schedule_type == "once":
                    s.enabled = False
                    s.next_run_at = None
                else:
                    s.next_run_at = s.compute_next_run_at(now)
                s.last_run_at = now
                s.locked_until = None
                s.save(update_fields=["last_status", "last_error", "enabled", "next_run_at", "last_run_at", "locked_until"])
                return

            qs = TestCase.objects.filter(id__in=case_ids)
            if s.project_id:
                qs = qs.filter(project_id=s.project_id)
            if s.created_by_id:
                qs = qs.filter(project__in=visible_projects(s.created_by))
            cases = list(qs)
            if not cases:
                s.last_status = "no_visible_cases"
                s.last_error = "no cases matched or not visible"
                if s.schedule_type == "once":
                    s.enabled = False
                    s.next_run_at = None
                else:
                    s.next_run_at = s.compute_next_run_at(now)
                s.last_run_at = now
                s.locked_until = None
                s.save(update_fields=["last_status", "last_error", "enabled", "next_run_at", "last_run_at", "locked_until"])
                return

            created = []
            for case in cases:
                if getattr(case, "case_mode", "normal") == "advanced":
                    params = getattr(case, "parameters", {}) or {}
                    datasets = params.get("datasets") if isinstance(params, dict) else None
                    if not isinstance(datasets, list) or not datasets:
                        continue
                    try:
                        max_runs = int((params.get("max_runs") if isinstance(params, dict) else None) or 0)
                    except Exception:
                        max_runs = 0
                    if max_runs <= 0:
                        max_runs = 10
                    batch_id = uuid.uuid4()
                    expanded = []
                    for ds in datasets:
                        expanded.extend(expand_dataset(ds, max_runs=max_runs))
                        if len(expanded) >= max_runs:
                            expanded = expanded[:max_runs]
                            break
                    run_total = min(len(expanded), max_runs)
                    for idx, ds in enumerate(expanded[:run_total]):
                        if not isinstance(ds, dict):
                            continue
                        name = str(ds.get("name") or f"数据集{idx+1}")[:120]
                        vars_obj = ds.get("vars") or {}
                        if not isinstance(vars_obj, dict):
                            vars_obj = {}
                        ex = AutoTestExecution.objects.create(
                            case=case,
                            executor=s.created_by,
                            status="pending",
                            batch_id=batch_id,
                            run_index=idx + 1,
                            run_total=run_total,
                            dataset_name=name,
                            dataset_vars=vars_obj,
                            trigger_source="schedule",
                            trigger_payload={"schedule_id": int(s.id)},
                            schedule=s,
                        )
                        created.append(ex.id)
                        enqueue_execution(ex.id)
                else:
                    ex = AutoTestExecution.objects.create(
                        case=case,
                        executor=s.created_by,
                        status="pending",
                        trigger_source="schedule",
                        trigger_payload={"schedule_id": int(s.id)},
                        schedule=s,
                    )
                    created.append(ex.id)
                    enqueue_execution(ex.id)

            s.last_status = "ok" if created else "no_exec"
            s.last_error = ""
            s.last_run_at = now
            if s.schedule_type == "once":
                s.enabled = False
                s.next_run_at = None
            else:
                s.next_run_at = s.compute_next_run_at(now)
            s.locked_until = None
            s.save(update_fields=["last_status", "last_error", "last_run_at", "enabled", "next_run_at", "locked_until"])
        except Exception as e:
            try:
                s = AutoTestSchedule.objects.get(id=schedule_id)
                s.last_status = "error"
                s.last_error = str(e)[:800]
                s.last_run_at = now
                if s.schedule_type == "once":
                    s.enabled = False
                    s.next_run_at = None
                else:
                    s.next_run_at = s.compute_next_run_at(now)
                s.locked_until = None
                s.save(update_fields=["last_status", "last_error", "last_run_at", "enabled", "next_run_at", "locked_until"])
            except Exception:
                pass
