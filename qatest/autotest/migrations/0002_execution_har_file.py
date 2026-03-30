from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("autotest", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="autotestexecution",
            name="har_file",
            field=models.FileField(blank=True, null=True, upload_to="autotest/har/", verbose_name="HAR文件"),
        ),
    ]

