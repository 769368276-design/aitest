from django.db import models
from django.conf import settings
from projects.models import Project
from requirements.models import Requirement

class AIGenerationJob(models.Model):
    STATUS_CHOICES = [
        ("running", "运行中"),
        ("stopped", "已停止"),
        ("done", "已完成"),
        ("error", "失败"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_generation_jobs", verbose_name="用户")
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="项目")
    requirement = models.ForeignKey(Requirement, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="需求")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="running", verbose_name="状态")
    progress_message = models.CharField(max_length=255, blank=True, default="", verbose_name="进度信息")
    error_message = models.TextField(blank=True, default="", verbose_name="错误信息")
    cancel_requested = models.BooleanField(default=False, verbose_name="请求停止")

    source_name = models.CharField(max_length=255, blank=True, default="", verbose_name="源文件名")
    source_path = models.CharField(max_length=500, blank=True, default="", verbose_name="源文件路径")
    markdown_text = models.TextField(blank=True, default="", verbose_name="生成原文")
    cases_json = models.JSONField(blank=True, default=list, verbose_name="结构化用例")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "ai_generation_jobs"
        verbose_name = "AI生成任务"
        verbose_name_plural = "AI生成任务"
        ordering = ["-created_at"]
