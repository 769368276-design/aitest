from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("testcases", "0007_alter_testcase_requirement"),
    ]

    operations = [
        migrations.AddField(
            model_name="testcase",
            name="case_mode",
            field=models.CharField(default="normal", max_length=20, verbose_name="用例模式"),
        ),
        migrations.AddField(
            model_name="testcase",
            name="parameters",
            field=models.JSONField(blank=True, default=dict, verbose_name="参数集"),
        ),
    ]

