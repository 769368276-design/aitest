from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.models import Group, Permission

from users.models import User, UserGroup, UserGroupMember
from projects.models import Project


class UsersSmokeTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin", password="pass1234", is_staff=True)
        self.client.force_login(self.admin)

        Group.objects.get_or_create(name="System Admin")
        Group.objects.get_or_create(name="Tester")
        self.project = Project.objects.create(name="P1", owner=self.admin)

    def test_user_manage_page_and_create_user(self):
        resp = self.client.get(reverse("user_manage"))
        self.assertEqual(resp.status_code, 200)

        resp2 = self.client.post(
            reverse("user_manage"),
            {
                "username": "u1",
                "password1": "pass1234",
                "password2": "pass1234",
                "role": "",
                "is_staff": "",
            },
            follow=True,
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(User.objects.filter(username="u1").exists())

    def test_role_pages(self):
        resp = self.client.get(reverse("role_list"))
        self.assertEqual(resp.status_code, 200)

        perm = Permission.objects.order_by("id").first()
        resp2 = self.client.post(
            reverse("role_create"),
            {"name": "MyRole", "permissions": [perm.id] if perm else []},
            follow=True,
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(Group.objects.filter(name="MyRole").exists())

    def test_user_group_flow(self):
        resp = self.client.get(reverse("user_group_list"))
        self.assertEqual(resp.status_code, 200)

        resp2 = self.client.post(
            reverse("user_group_list"),
            {"name": "G1", "shared_projects": [self.project.id]},
            follow=True,
        )
        self.assertEqual(resp2.status_code, 200)
        g = UserGroup.objects.get(name="G1", owner=self.admin)
        self.assertTrue(UserGroupMember.objects.filter(group=g, user=self.admin).exists())

        member = User.objects.create_user(username="m1", password="pass1234")
        resp3 = self.client.post(
            reverse("user_group_add_member", kwargs={"group_id": g.id}),
            {"username": "m1", "role": "member"},
            follow=True,
        )
        self.assertEqual(resp3.status_code, 200)
        self.assertTrue(UserGroupMember.objects.filter(group=g, user=member).exists())
