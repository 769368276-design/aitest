from django.test import TestCase
from users.models import User


class DiagnosticsPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", password="pass1234")

    def test_requires_login(self):
        resp = self.client.get("/diagnostics/")
        self.assertIn(resp.status_code, (302, 301))

    def test_page_ok(self):
        self.client.login(username="u1", password="pass1234")
        resp = self.client.get("/diagnostics/")
        self.assertEqual(resp.status_code, 200)
