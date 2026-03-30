from django.test import TestCase
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile

from users.models import User
from projects.models import Project
from projects.models import ProjectMember
from testcases.models import TestCase as Case, TestCaseStep


class StepGuideUploadTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.client.force_login(self.user)
        self.project = Project.objects.create(name="P1", owner=self.user)
        self.case = Case.objects.create(project=self.project, title="C1", creator=self.user)
        self.step = TestCaseStep.objects.create(case=self.case, step_number=1, description="d", expected_result="e")

    def test_upload_step_guide_saves_base64(self):
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf"
            b"\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb1\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        f = SimpleUploadedFile("t.png", png_bytes, content_type="image/png")
        url = reverse("upload_step_guide", kwargs={"step_id": self.step.id})
        resp = self.client.post(url, {"guide_image": f})
        self.assertEqual(resp.status_code, 200)
        self.step.refresh_from_db()
        self.assertTrue(bool(self.step.guide_image_base64))


class StepTransferFileUploadTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.client.force_login(self.user)
        self.project = Project.objects.create(name="P1", owner=self.user)
        self.case = Case.objects.create(project=self.project, title="C1", creator=self.user)
        self.step = TestCaseStep.objects.create(case=self.case, step_number=1, description="d", expected_result="e")

    def test_upload_transfer_file_saves_base64(self):
        raw = b"hello world"
        f = SimpleUploadedFile("a.txt", raw, content_type="text/plain")
        url = reverse("upload_step_transfer_file", kwargs={"step_id": self.step.id})
        resp = self.client.post(url, {"transfer_file": f})
        self.assertEqual(resp.status_code, 200)
        self.step.refresh_from_db()
        self.assertEqual(self.step.transfer_file_name, "a.txt")
        self.assertTrue(bool(self.step.transfer_file_base64))


class TestCasesSmokeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.client.force_login(self.user)
        self.project = Project.objects.create(name="P1", owner=self.user)

    def test_case_create_with_steps(self):
        resp = self.client.get(reverse("case_create"))
        self.assertEqual(resp.status_code, 200)

        upload = SimpleUploadedFile("doc.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        resp2 = self.client.post(
            reverse("case_create"),
            {
                "project": self.project.id,
                "requirement": "",
                "title": "C1",
                "pre_condition": "",
                "type": 1,
                "execution_type": 1,
                "priority": 2,
                "status": 0,
                "case_mode": "normal",
                "parameters": "{}",
                "steps-TOTAL_FORMS": "1",
                "steps-INITIAL_FORMS": "0",
                "steps-MIN_NUM_FORMS": "0",
                "steps-MAX_NUM_FORMS": "1000",
                "steps-0-step_number": "1",
                "steps-0-description": "打开登录页",
                "steps-0-expected_result": "页面打开",
                "steps-0-transfer_file_upload": upload,
            },
            follow=True,
        )
        self.assertEqual(resp2.status_code, 200)
        case = Case.objects.get(title="C1")
        self.assertEqual(case.steps.count(), 1)
        step = case.steps.first()
        self.assertTrue(bool(step.transfer_file_base64))

        resp3 = self.client.get(reverse("case_detail", kwargs={"pk": case.id}))
        self.assertEqual(resp3.status_code, 200)

    def test_case_copy_creates_new_case_and_steps(self):
        case = Case.objects.create(project=self.project, title="Cbase", creator=self.user, status=0)
        TestCaseStep.objects.create(
            case=case,
            step_number=1,
            description="上传文件",
            expected_result="成功",
            guide_image_base64="",
            transfer_file_name="a.txt",
            transfer_file_content_type="text/plain",
            transfer_file_size=11,
            transfer_file_base64="aGVsbG8gd29ybGQ=",
        )
        url = reverse("case_copy", kwargs={"pk": case.id})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("success"))
        new_id = data.get("new_id")
        self.assertTrue(bool(new_id))
        copied = Case.objects.get(id=new_id)
        self.assertNotEqual(copied.id, case.id)
        self.assertEqual(copied.project_id, case.project_id)
        self.assertEqual(copied.status, 0)
        self.assertEqual(copied.steps.count(), 1)
        s2 = copied.steps.first()
        self.assertEqual(s2.transfer_file_name, "a.txt")
        self.assertTrue(bool(s2.transfer_file_base64))

    def test_delete_step_reorders_numbers(self):
        case = Case.objects.create(project=self.project, title="Cdel", creator=self.user, status=0)
        s1 = TestCaseStep.objects.create(case=case, step_number=1, description="1", expected_result="1")
        s2 = TestCaseStep.objects.create(case=case, step_number=2, description="2", expected_result="2")
        s3 = TestCaseStep.objects.create(case=case, step_number=3, description="3", expected_result="3")
        resp = self.client.post(reverse("delete_step", kwargs={"step_id": s2.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(case.steps.count(), 2)
        nums = list(case.steps.order_by("step_number").values_list("step_number", flat=True))
        self.assertEqual(nums, [1, 2])
        self.assertEqual(case.steps.order_by("step_number").first().id, s1.id)
        self.assertEqual(case.steps.order_by("step_number").last().id, s3.id)

    def test_delete_step_requires_edit_permission(self):
        other = User.objects.create_user(username="u2", password="pass1234", is_staff=False)
        ProjectMember.objects.create(project=self.project, user=other, role=None)
        case = Case.objects.create(project=self.project, title="Cperm", creator=self.user, status=0)
        step = TestCaseStep.objects.create(case=case, step_number=1, description="1", expected_result="1")
        self.client.force_login(other)
        resp = self.client.post(reverse("delete_step", kwargs={"step_id": step.id}))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(TestCaseStep.objects.filter(id=step.id).exists())

    def test_case_edit_ignores_blank_extra_row_with_step_number(self):
        case = Case.objects.create(project=self.project, title="Cold", creator=self.user, status=0)
        step = TestCaseStep.objects.create(case=case, step_number=1, description="d1", expected_result="e1")
        resp = self.client.post(
            reverse("case_edit", kwargs={"pk": case.id}),
            {
                "project": self.project.id,
                "requirement": "",
                "title": "Cnew",
                "pre_condition": "",
                "type": 1,
                "execution_type": 1,
                "priority": 2,
                "status": 0,
                "case_mode": "normal",
                "parameters": "{}",
                "steps-TOTAL_FORMS": "2",
                "steps-INITIAL_FORMS": "1",
                "steps-MIN_NUM_FORMS": "0",
                "steps-MAX_NUM_FORMS": "1000",
                "steps-0-id": str(step.id),
                "steps-0-step_number": "1",
                "steps-0-description": "d1 changed",
                "steps-0-expected_result": "e1 changed",
                "steps-1-id": "",
                "steps-1-step_number": "2",
                "steps-1-description": "",
                "steps-1-expected_result": "",
            },
            follow=False,
        )
        self.assertEqual(resp.status_code, 302)
        case.refresh_from_db()
        self.assertEqual(case.title, "Cnew")
        step.refresh_from_db()
        self.assertEqual(step.description, "d1 changed")

    def test_case_edit_upload_guide_image_should_not_500(self):
        case = Case.objects.create(project=self.project, title="Cimg", creator=self.user, status=0)
        step = TestCaseStep.objects.create(case=case, step_number=1, description="d1", expected_result="e1")
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf"
            b"\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb1\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        upload = SimpleUploadedFile("guide.png", png_bytes, content_type="image/png")
        resp = self.client.post(
            reverse("case_edit", kwargs={"pk": case.id}),
            {
                "project": self.project.id,
                "requirement": "",
                "title": "Cimg2",
                "pre_condition": "",
                "type": 1,
                "execution_type": 1,
                "priority": 2,
                "status": 0,
                "case_mode": "normal",
                "parameters": "{}",
                "steps-TOTAL_FORMS": "1",
                "steps-INITIAL_FORMS": "1",
                "steps-MIN_NUM_FORMS": "0",
                "steps-MAX_NUM_FORMS": "1000",
                "steps-0-id": str(step.id),
                "steps-0-step_number": "1",
                "steps-0-description": "d1 changed",
                "steps-0-expected_result": "e1 changed",
                "steps-0-guide_image": upload,
            },
            follow=False,
        )
        self.assertNotEqual(resp.status_code, 500)

    def test_case_edit_respects_submitted_step_order(self):
        case = Case.objects.create(project=self.project, title="Csort", creator=self.user, status=0)
        s1 = TestCaseStep.objects.create(case=case, step_number=1, description="first", expected_result="e1")
        s2 = TestCaseStep.objects.create(case=case, step_number=2, description="second", expected_result="e2")
        resp = self.client.post(
            reverse("case_edit", kwargs={"pk": case.id}),
            {
                "project": self.project.id,
                "requirement": "",
                "title": "Csort",
                "pre_condition": "",
                "type": 1,
                "execution_type": 1,
                "priority": 2,
                "status": 0,
                "case_mode": "normal",
                "parameters": "{}",
                "steps-TOTAL_FORMS": "2",
                "steps-INITIAL_FORMS": "2",
                "steps-MIN_NUM_FORMS": "0",
                "steps-MAX_NUM_FORMS": "1000",
                "steps-0-id": str(s1.id),
                "steps-0-step_number": "2",
                "steps-0-description": "first",
                "steps-0-expected_result": "e1",
                "steps-1-id": str(s2.id),
                "steps-1-step_number": "1",
                "steps-1-description": "second",
                "steps-1-expected_result": "e2",
            },
            follow=False,
        )
        self.assertEqual(resp.status_code, 302)
        s1.refresh_from_db()
        s2.refresh_from_db()
        self.assertEqual(s2.step_number, 1)
        self.assertEqual(s1.step_number, 2)
