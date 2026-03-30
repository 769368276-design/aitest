from __future__ import annotations

import re

from django.core.management.base import BaseCommand

from testcases.models import TestCaseStep


def _infer_smart_on(desc: str) -> bool:
    s = str(desc or "").strip()
    if not s:
        return False
    sl = s.lower()
    if not (
        any(k in s for k in ["用户名", "账号", "密码", "手机号", "手机", "电话", "邮箱"])
        or any(k in sl for k in ["username", "account", "login", "password", "passwd", "phone", "mobile", "tel", "email"])
    ):
        return False
    if re.search(r"[:：=]\s*[^\s，,；;。]{2,}", s):
        return True
    if re.search(r"\b\d{6,}\b", s):
        return True
    if re.search(r"\b[A-Za-z0-9_]{4,}\b", s):
        return True
    return True


class Command(BaseCommand):
    help = "Backfill smart_data_enabled for existing steps that look like they contain example credentials/phones/emails."

    def add_arguments(self, parser):
        parser.add_argument("--case-id", type=int, default=0)
        parser.add_argument("--project-id", type=int, default=0)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        case_id = int(options.get("case_id") or 0)
        project_id = int(options.get("project_id") or 0)
        dry_run = bool(options.get("dry_run"))

        qs = TestCaseStep.objects.filter(smart_data_enabled=False).select_related("case", "case__project")
        if case_id > 0:
            qs = qs.filter(case_id=case_id)
        if project_id > 0:
            qs = qs.filter(case__project_id=project_id)

        hit_ids = []
        for s in qs.iterator(chunk_size=500):
            try:
                if _infer_smart_on(getattr(s, "description", "") or ""):
                    hit_ids.append(int(s.id))
            except Exception:
                continue

        if dry_run:
            self.stdout.write(f"matched_steps={len(hit_ids)} dry_run=1")
            return

        updated = 0
        if hit_ids:
            updated = TestCaseStep.objects.filter(id__in=hit_ids).update(smart_data_enabled=True)
        self.stdout.write(f"matched_steps={len(hit_ids)} updated={int(updated)}")

