from django.test import TestCase
from django.urls import reverse

from users.models import User
from projects.models import Project
from requirements.models import Requirement


class RequirementsSmokeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.client.force_login(self.user)
        self.project = Project.objects.create(name="P1", owner=self.user)

    def test_requirement_crud_smoke(self):
        resp = self.client.get(reverse("requirement_list"))
        self.assertEqual(resp.status_code, 200)

        resp2 = self.client.post(
            reverse("requirement_create"),
            {
                "project": self.project.id,
                "title": "R1",
                "description": "desc",
                "type": 1,
                "priority": 2,
                "status": 1,
                "expected_finish_time": "",
            },
            follow=True,
        )
        self.assertEqual(resp2.status_code, 200)
        r = Requirement.objects.get(title="R1")

        resp3 = self.client.get(reverse("requirement_detail", kwargs={"pk": r.id}))
        self.assertEqual(resp3.status_code, 200)

        resp4 = self.client.post(
            reverse("requirement_edit", kwargs={"pk": r.id}),
            {
                "project": self.project.id,
                "title": "R1a",
                "description": "desc2",
                "type": 1,
                "priority": 1,
                "status": 2,
                "expected_finish_time": "",
            },
            follow=True,
        )
        self.assertEqual(resp4.status_code, 200)
        r.refresh_from_db()
        self.assertEqual(r.title, "R1a")
