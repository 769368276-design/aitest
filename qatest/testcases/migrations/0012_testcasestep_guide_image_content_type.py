from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("testcases", "0011_testcasestep_transfer_file_base64_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="testcasestep",
            name="guide_image_content_type",
            field=models.CharField(blank=True, default="", max_length=120, verbose_name="参考截图类型"),
        ),
    ]

