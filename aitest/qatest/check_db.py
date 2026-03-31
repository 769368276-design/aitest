import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'qa_platform.settings')
django.setup()

from testcases.models import TestCase, TestCaseStep
from users.models import User

user = User.objects.filter(username='admin').first()
c = TestCase.objects.filter(creator=user).first()

if not c:
    print("No test case found!")
    exit(1)

print(f"Test Case: {c.title}")
print(f"Total steps: {c.steps.count()}")

steps = TestCaseStep.objects.filter(case=c).order_by('step_number')
for s in steps:
    has_guide = bool(s.guide_image_base64 or s.guide_image)
    has_transfer = bool(s.transfer_file_base64 or s.transfer_file_name)
    print(f"\nStep {s.step_number}: {s.description[:50]}")
    print(f"  Guide image: {has_guide} (base64: {bool(s.guide_image_base64)}, image: {s.guide_image}, content_type: {s.guide_image_content_type})")
    print(f"  Transfer file: {has_transfer} (base64: {bool(s.transfer_file_base64)}, name: {s.transfer_file_name})")
