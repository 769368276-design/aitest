from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0004_useraimodelconfig"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserCICredential",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.CharField(max_length=80, unique=True, verbose_name="Token")),
                ("enabled", models.BooleanField(default=True, verbose_name="启用")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("last_used_at", models.DateTimeField(blank=True, null=True, verbose_name="最近使用时间")),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ci_credential",
                        to="users.user",
                        verbose_name="用户",
                    ),
                ),
            ],
            options={
                "verbose_name": "CI/CD 凭证",
                "verbose_name_plural": "CI/CD 凭证",
            },
        ),
    ]

