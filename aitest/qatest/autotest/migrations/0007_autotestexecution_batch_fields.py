from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("autotest", "0006_autoteststeprecord_ocr_screenshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="autotestexecution",
            name="batch_id",
            field=models.UUIDField(blank=True, null=True, verbose_name="批次ID"),
        ),
        migrations.AddField(
            model_name="autotestexecution",
            name="run_index",
            field=models.PositiveIntegerField(default=1, verbose_name="轮次序号"),
        ),
        migrations.AddField(
            model_name="autotestexecution",
            name="run_total",
            field=models.PositiveIntegerField(default=1, verbose_name="轮次总数"),
        ),
        migrations.AddField(
            model_name="autotestexecution",
            name="dataset_name",
            field=models.CharField(blank=True, max_length=120, verbose_name="数据集名称"),
        ),
        migrations.AddField(
            model_name="autotestexecution",
            name="dataset_vars",
            field=models.JSONField(blank=True, default=dict, verbose_name="数据集变量"),
        ),
    ]

