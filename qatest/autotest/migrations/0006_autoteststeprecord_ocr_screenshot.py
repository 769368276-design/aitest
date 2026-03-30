from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("autotest", "0005_autoteststeprecord_metrics"),
    ]

    operations = [
        migrations.AddField(
            model_name="autoteststeprecord",
            name="ocr_screenshot",
            field=models.ImageField(blank=True, null=True, upload_to="autotest/screenshots/", verbose_name="OCR截图"),
        ),
    ]

