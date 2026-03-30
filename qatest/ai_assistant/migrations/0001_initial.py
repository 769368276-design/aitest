from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projects", "0004_project_base_url_project_history_requirements_and_more"),
        ("requirements", "0003_alter_requirement_options_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="AIGenerationJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("running", "运行中"), ("stopped", "已停止"), ("done", "已完成"), ("error", "失败")], default="running", max_length=20, verbose_name="状态")),
                ("progress_message", models.CharField(blank=True, default="", max_length=255, verbose_name="进度信息")),
                ("error_message", models.TextField(blank=True, default="", verbose_name="错误信息")),
                ("cancel_requested", models.BooleanField(default=False, verbose_name="请求停止")),
                ("source_name", models.CharField(blank=True, default="", max_length=255, verbose_name="源文件名")),
                ("source_path", models.CharField(blank=True, default="", max_length=500, verbose_name="源文件路径")),
                ("markdown_text", models.TextField(blank=True, default="", verbose_name="生成原文")),
                ("cases_json", models.JSONField(blank=True, default=list, verbose_name="结构化用例")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("project", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="projects.project", verbose_name="项目")),
                ("requirement", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="requirements.requirement", verbose_name="需求")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ai_generation_jobs", to=settings.AUTH_USER_MODEL, verbose_name="用户")),
            ],
            options={
                "verbose_name": "AI生成任务",
                "verbose_name_plural": "AI生成任务",
                "db_table": "ai_generation_jobs",
                "ordering": ["-created_at"],
            },
        ),
    ]

