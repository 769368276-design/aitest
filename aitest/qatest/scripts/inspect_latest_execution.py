import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "qa_platform.settings")

import django  # noqa: E402

django.setup()  # noqa: E402

from autotest.models import AutoTestExecution, AutoTestStepRecord  # noqa: E402
from testcases.models import TestCaseStep  # noqa: E402


def main() -> None:
    exe = AutoTestExecution.objects.order_by("-id").first()
    if not exe:
        print("No AutoTestExecution found.")
        return
    print("Execution:", exe.id, exe.status)
    try:
        print("Updated:", getattr(exe, "updated_at", None))
    except Exception:
        pass
    print("Summary:", exe.result_summary)
    try:
        case = exe.case
    except Exception:
        case = None
    if case:
        steps = list(TestCaseStep.objects.filter(case=case).order_by("step_number"))
        print("Case steps:", len(steps))
        for s in steps[:60]:
            try:
                b64_raw = getattr(s, "transfer_file_base64", None)
                b64 = bool(str(b64_raw or "").strip())
                name = str(getattr(s, "transfer_file_name", "") or "")
                size = int(getattr(s, "transfer_file_size", 0) or 0)
                if b64 or name:
                    print("  step", s.step_number, "transfer_file:", name, size, "has_base64=", b64, "b64_len=", len(str(b64_raw or "")))
            except Exception:
                pass
    qs = AutoTestStepRecord.objects.filter(execution=exe).order_by("step_number")
    rows = list(qs[:200])
    tail = rows[-30:] if len(rows) > 30 else rows
    for r in tail:
        print("Step", r.step_number, r.status, r.description)
        try:
            print("  updated:", getattr(r, "updated_at", None))
        except Exception:
            pass
        if r.error_message:
            print("  error:", r.error_message[:500])
        m = r.metrics if isinstance(r.metrics, dict) else {}
        if m:
            for k in ["transfer_file_prefill", "filechooser_autofill", "file_input_pick", "transfer_file_selection", "transfer_file_level"]:
                if k in m:
                    print("  metric", k + ":", m.get(k))


if __name__ == "__main__":
    main()
