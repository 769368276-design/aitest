from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group
from django.contrib.auth import get_user_model
import os
import secrets

class Command(BaseCommand):
    help = 'Initialize default data'

    def handle(self, *args, **options):
        roles = ['System Admin', 'Project Admin', 'Tester', 'Developer', 'Normal User']
        for role in roles:
            Group.objects.get_or_create(name=role)
            self.stdout.write(self.style.SUCCESS(f'Role "{role}" created/exists'))

        User = get_user_model()
        if not User.objects.filter(username='admin').exists():
            try:
                admin_role = Group.objects.get(name='System Admin')
                admin_password = os.getenv("INIT_ADMIN_PASSWORD") or secrets.token_urlsafe(16)
                user = User.objects.create_superuser('admin', 'admin@example.com', admin_password)
                user.role = admin_role
                user.save()
                self.stdout.write(self.style.SUCCESS('Superuser "admin" created'))
                if not os.getenv("INIT_ADMIN_PASSWORD"):
                    self.stdout.write(self.style.WARNING(f'Generated admin password: {admin_password}'))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Error creating superuser: {e}'))
        else:
            self.stdout.write(self.style.SUCCESS('Superuser "admin" already exists'))
