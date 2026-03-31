import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'qa_platform.settings')
django.setup()

from autotest.models import AutoTestExecution, AutoTestStepRecord

# Check execution ID 18
try:
    e = AutoTestExecution.objects.get(id=18)
    print(f"=== Execution ID 18 ===")
    print(f"ID: {e.id}")
    print(f"Status: {e.status}")
    print(f"Created: {e.start_time}")
    print(f"Error: {e.result_summary.get('error', 'None') if e.result_summary else 'None'}")
    print(f"Summary: {e.result_summary}")
    print(f"\n=== Step Records ===")
    steps = AutoTestStepRecord.objects.filter(execution=e).order_by('step_number')
    print(f"Total steps: {steps.count()}")
    for s in steps:
        print(f"Step {s.step_number}: {s.status} - {s.description[:50] if s.description else 'N/A'}")
        if s.error_message:
            print(f"  Error: {s.error_message[:200]}")
except Exception as ex:
    print(f"Error: {ex}")
