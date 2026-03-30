import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'qa_platform.settings')
django.setup()

from testcases.models import TestCase, TestCaseStep
from testcases.views import TestCaseStepForm, TestCaseStepFormSet
from users.models import User

# Get test data
user = User.objects.filter(username='admin').first()
c = TestCase.objects.filter(creator=user).first()

if not c:
    print("No test case found!")
    exit(1)

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
print(f"Before: Step {s.id}, guide_image_base64={bool(s.guide_image_base64)}, content_type='{s.guide_image_content_type}'")

# Simulate formset submission with clear checkbox
# This simulates what happens in the actual form POST
formset_data = {
    'steps-TOTAL_FORMS': '1',
    'steps-INITIAL_FORMS': '1',
    'steps-MIN_NUM_FORMS': '0',
    'steps-MAX_NUM_FORMS': '1000',
    'steps-0-id': str(s.id),
    'steps-0-step_number': '1',
    'steps-0-description': s.description,
    'steps-0-expected_result': s.expected_result,
    'steps-0-guide_image_clear': 'on',  # Checkbox is checked
}

formset = TestCaseStepFormSet(data=formset_data, instance=c)
print(f"\nFormset is valid: {formset.is_valid()}")
if not formset.is_valid():
    print(f"Formset errors: {formset.non_form_errors()}")
    for i, form in enumerate(formset.forms):
        if form.errors:
            print(f"Form {i} errors: {form.errors}")
else:
    for i, form in enumerate(formset.forms):
        print(f"\nForm {i}:")
        print(f"  has_changed: {form.has_changed()}")
        print(f"  guide_image_clear in data: {'steps-0-guide_image_clear' in formset.data}")
        print(f"  guide_image_clear value: {formset.data.get('steps-0-guide_image_clear')}")
        if form.is_valid():
            print(f"  Cleaned guide_image_clear: {form.cleaned_data.get('guide_image_clear')}")
        else:
            print(f"  Form errors: {form.errors}")
    
    # Save the formset
    formset.save()
    
    # Check database
    s.refresh_from_db()
    print(f"\nAfter save (from DB): guide_image_base64={bool(s.guide_image_base64)}, content_type='{s.guide_image_content_type}', guide_image={s.guide_image}")
    
    if not s.guide_image_base64 and not s.guide_image_content_type and not s.guide_image:
        print("\n✅ SUCCESS: Guide image cleared successfully via formset!")
    else:
        print("\n❌ FAILED: Guide image still exists in database!")
