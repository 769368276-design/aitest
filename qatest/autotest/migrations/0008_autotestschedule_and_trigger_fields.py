import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0004_project_base_url_project_history_requirements_and_more"),
        ("autotest", "0007_autotestexecution_batch_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="AutoTestSchedule",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, verbose_name="计划名称")),
                ("enabled", models.BooleanField(default=True, verbose_name="启用")),
                (
                    "schedule_type",
                    models.CharField(
                        choices=[("interval", "按间隔"), ("daily", "每天定点"), ("once", "单次")],
                        default="interval",
                        max_length=20,
                        verbose_name="计划类型",
                    ),
                ),
                ("interval_minutes", models.PositiveIntegerField(default=60, verbose_name="间隔(分钟)")),
                ("daily_time", models.TimeField(blank=True, null=True, verbose_name="每天时间")),
                ("run_at", models.DateTimeField(blank=True, null=True, verbose_name="执行时间(单次)")),
                ("case_ids", models.JSONField(blank=True, default=list, verbose_name="用例ID列表")),
                ("next_run_at", models.DateTimeField(blank=True, null=True, verbose_name="下次执行时间")),
                ("last_run_at", models.DateTimeField(blank=True, null=True, verbose_name="上次执行时间")),
                ("locked_until", models.DateTimeField(blank=True, null=True, verbose_name="锁定到期")),
                ("last_status", models.CharField(blank=True, default="", max_length=30, verbose_name="上次状态")),
                ("last_error", models.TextField(blank=True, default="", verbose_name="上次错误")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="创建人",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="autotest_schedules",
                        to="projects.project",
                        verbose_name="项目",
                    ),
                ),
            ],
            options={
                "verbose_name": "自动化计划任务",
                "verbose_name_plural": "自动化计划任务",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddField(
            model_name="autotestexecution",
            name="trigger_source",
            field=models.CharField(blank=True, default="", max_length=30, verbose_name="触发来源"),
        ),
        migrations.AddField(
            model_name="autotestexecution",
            name="trigger_payload",
            field=models.JSONField(blank=True, default=dict, verbose_name="触发参数"),
        ),
        migrations.AddField(
            model_name="autotestexecution",
            name="schedule",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="executions",
                to="autotest.autotestschedule",
                verbose_name="计划任务",
            ),
        ),
    ]

