import json
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from ai_assistant.models import AIGenerationJob
from projects.models import Project
from requirements.models import Requirement


User = get_user_model()


class AIGenerationJobApiUnitTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pass1234", is_staff=True)
        self.project = Project.objects.create(name="P1", owner=self.user, base_url="https://example.com")
        self.requirement = Requirement.objects.create(project=self.project, title="R1", description="")
        self.client.login(username="u1", password="pass1234")

    def test_job_status_and_poll(self):
        job = AIGenerationJob.objects.create(
            user=self.user,
            project=self.project,
            requirement=self.requirement,
            status="done",
            progress_message="done",
            markdown_text="Hello\nWorld\n",
            cases_json=[{"title": "TC-001", "steps": []}],
        )

        resp = self.client.get(reverse("ai_job_status", kwargs={"job_id": job.id}))
        self.assertEqual(resp.status_code, 200)
        data = resp.json() or {}
        self.assertTrue(data.get("success"))
        self.assertIn("cases", data)
        self.assertIn("markdown_text", data)

        resp2 = self.client.get(reverse("ai_job_poll", kwargs={"job_id": job.id}) + "?offset=0")
        self.assertEqual(resp2.status_code, 200)
        d2 = resp2.json() or {}
        self.assertTrue(d2.get("success"))
        self.assertEqual(d2.get("delta"), "Hello\nWorld\n")
        self.assertEqual(int(d2.get("next_offset") or 0), len("Hello\nWorld\n"))

    def test_job_stop_sets_flag(self):
        job = AIGenerationJob.objects.create(user=self.user, project=self.project, status="running", progress_message="start")
        resp = self.client.post(reverse("ai_job_stop", kwargs={"job_id": job.id}), data=json.dumps({}), content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        job.refresh_from_db()
        self.assertTrue(job.cancel_requested)

    def test_job_clear_deletes(self):
        job = AIGenerationJob.objects.create(
            user=self.user,
            project=self.project,
            requirement=self.requirement,
            status="done",
            progress_message="done",
            markdown_text="x",
            cases_json=[{"title": "TC-001", "steps": []}],
        )
        resp = self.client.post(reverse("ai_job_clear", kwargs={"job_id": job.id}), data=json.dumps({}), content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AIGenerationJob.objects.filter(id=job.id).exists())
