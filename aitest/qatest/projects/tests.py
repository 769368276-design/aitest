from django.test import TestCase
from django.urls import reverse

from users.models import User
from projects.models import Project


class ProjectsSmokeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.client.force_login(self.user)

    def test_project_crud_smoke(self):
        resp = self.client.get(reverse("project_list"))
        self.assertEqual(resp.status_code, 200)

        resp2 = self.client.post(
            reverse("project_create"),
            {
                "name": "P1",
                "description": "D",
                "base_url": "https://example.com",
                "test_accounts": "",
                "history_requirements": "",
                "knowledge_base": "",
                "status": 1,
                "start_time": "",
                "end_time": "",
            },
            follow=True,
        )
        self.assertEqual(resp2.status_code, 200)
        p = Project.objects.get(name="P1")

        resp3 = self.client.get(reverse("project_detail", kwargs={"pk": p.id}))
        self.assertEqual(resp3.status_code, 200)

        resp4 = self.client.post(
            reverse("project_edit", kwargs={"pk": p.id}),
            {
                "name": "P1a",
                "description": "D2",
                "base_url": "https://example.com",
                "test_accounts": "",
                "history_requirements": "",
                "knowledge_base": "",
                "status": 2,
                "start_time": "",
                "end_time": "",
            },
            follow=True,
        )
        self.assertEqual(resp4.status_code, 200)
        p.refresh_from_db()
        self.assertEqual(p.name, "P1a")
