from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("testcases", "0012_testcasestep_guide_image_content_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="testcasestep",
            name="smart_data_enabled",
            field=models.BooleanField(default=False, verbose_name="智能数据生成"),
        ),
    ]

