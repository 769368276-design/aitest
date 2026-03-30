from django.test import TestCase
from django.urls import reverse

from users.models import User
from projects.models import Project
from testcases.models import TestCase as Case
from bugs.models import Bug


class BugsSmokeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.client.force_login(self.user)
        self.project = Project.objects.create(name="P1", owner=self.user)
        self.case = Case.objects.create(project=self.project, title="C1", creator=self.user)

    def test_bug_crud_smoke(self):
        resp = self.client.get(reverse("bug_list"))
        self.assertEqual(resp.status_code, 200)

        resp2 = self.client.post(
            reverse("bug_create"),
            {
                "project": self.project.id,
                "case": self.case.id,
                "title": "B1",
                "description": "d",
                "reproduce_steps": "s",
                "severity": 3,
                "priority": 2,
                "status": 1,
                "assignee": self.user.id,
                "affected_version": "",
                "fixed_version": "",
            },
            follow=True,
        )
        self.assertEqual(resp2.status_code, 200)
        b = Bug.objects.get(title="B1")

        resp3 = self.client.get(reverse("bug_detail", kwargs={"pk": b.id}))
        self.assertEqual(resp3.status_code, 200)

        resp4 = self.client.post(
            reverse("bug_edit", kwargs={"pk": b.id}),
            {
                "project": self.project.id,
                "case": self.case.id,
                "title": "B1a",
                "description": "d2",
                "reproduce_steps": "s2",
                "severity": 2,
                "priority": 1,
                "status": 2,
                "assignee": self.user.id,
                "affected_version": "",
                "fixed_version": "",
            },
            follow=True,
        )
        self.assertEqual(resp4.status_code, 200)
        b.refresh_from_db()
        self.assertEqual(b.title, "B1a")
