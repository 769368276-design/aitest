from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("autotest", "0003_autotestreportshare"),
    ]

    operations = [
        migrations.AlterField(
            model_name="autotestexecution",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("queued", "Queued"),
                    ("running", "Running"),
                    ("paused", "Paused"),
                    ("stopped", "Stopped"),
                    ("completed", "Completed"),
                    ("failed", "Failed"),
                ],
                default="pending",
                max_length=20,
                verbose_name="状态",
            ),
        ),
    ]

