import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'qa_platform.settings')
django.setup()

from testcases.models import TestCase, TestCaseStep
from testcases.views import TestCaseStepForm
from users.models import User

# Get test data
user = User.objects.filter(username='admin').first()
c = TestCase.objects.filter(creator=user).first()
s = TestCaseStep.objects.filter(case=c).first()

if not s:
    print("No step found, creating one...")
    s = TestCaseStep.objects.create(
        case=c,
        step_number=1,
        description="Test step",
        expected_result="Test result"
    )

# Set guide image
s.guide_image_base64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='
s.guide_image_content_type = 'image/png'
s.save()
print(f"Before: guide_image_base64={bool(s.guide_image_base64)}, content_type='{s.guide_image_content_type}', guide_image={s.guide_image}")

# Simulate form submission with clear checkbox
form_data = {
    'step_number': '1',
    'description': s.description,
    'expected_result': s.expected_result,
    'guide_image_clear': 'on',  # This is what the checkbox sends when checked
}

form = TestCaseStepForm(data=form_data, instance=s)
print(f"\nForm is valid: {form.is_valid()}")
if not form.is_valid():
    print(f"Form errors: {form.errors}")
else:
    print(f"Cleaned guide_image: {form.cleaned_data.get('guide_image')}")
    print(f"Cleaned guide_image_clear: {form.cleaned_data.get('guide_image_clear')}")
    print(f"Form has_changed: {form.has_changed()}")
    
    saved = form.save()
    print(f"\nAfter save: guide_image_base64={bool(saved.guide_image_base64)}, content_type='{saved.guide_image_content_type}', guide_image={saved.guide_image}")
    
    # Verify from database
    saved.refresh_from_db()
    print(f"\nFrom DB: guide_image_base64={bool(saved.guide_image_base64)}, content_type='{saved.guide_image_content_type}', guide_image={saved.guide_image}")
    
    if not saved.guide_image_base64 and not saved.guide_image_content_type and not saved.guide_image:
        print("\n✅ SUCCESS: Guide image cleared successfully!")
    else:
        print("\n❌ FAILED: Guide image still exists!")
