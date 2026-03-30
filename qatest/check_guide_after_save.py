import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'qa_platform.settings')
django.setup()

from testcases.models import TestCase, TestCaseStep
from users.models import User

user = User.objects.filter(username='admin').first()
c = TestCase.objects.filter(creator=user, pk=1).first()

if not c:
    print("Test case not found!")
    exit(1)

print(f"Test Case: {c.title}")
print(f"Total steps: {c.steps.count()}")

steps = TestCaseStep.objects.filter(case=c).order_by('step_number')
for s in steps:
    has_guide = bool(s.guide_image_base64 or s.guide_image)
    if has_guide:
        print(f"\nStep {s.step_number}: {s.description[:50]}")
        print(f"  Guide image: {has_guide}")
        print(f"    - base64 exists: {bool(s.guide_image_base64)}")
        print(f"    - image file: {s.guide_image}")
        print(f"    - content_type: {s.guide_image_content_type}")
