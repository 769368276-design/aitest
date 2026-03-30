from django.conf import settings
from django.db import models

from testcases.models import TestCase, TestCaseStep


class UIAutoExecution(models.Model):
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("queued", "Queued"),
        ("running", "Running"),
        ("paused", "Paused"),
        ("stopped", "Stopped"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    )

    case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name="ui_auto_executions", verbose_name="测试用例")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", verbose_name="状态")
    executor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, verbose_name="执行人")
    start_time = models.DateTimeField(auto_now_add=True, verbose_name="开始时间")
    end_time = models.DateTimeField(null=True, blank=True, verbose_name="结束时间")
    result_summary = models.JSONField(default=dict, blank=True, verbose_name="结果汇总")

    stop_signal = models.BooleanField(default=False, verbose_name="停止信号")
    pause_signal = models.BooleanField(default=False, verbose_name="暂停信号")

    class Meta:
        verbose_name = "UI自动化执行记录"
        verbose_name_plural = "UI自动化执行记录"
        ordering = ["-start_time"]

    def __str__(self):
        return f"{self.case.title} - {self.status}"


class UIAutoStepRecord(models.Model):
    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("success", "Success"),
        ("failed", "Failed"),
        ("skipped", "Skipped"),
    )

    execution = models.ForeignKey(UIAutoExecution, on_delete=models.CASCADE, related_name="step_records", verbose_name="执行记录")
    step = models.ForeignKey(TestCaseStep, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="关联步骤")
    step_number = models.IntegerField(verbose_name="步骤序号")
    description = models.TextField(verbose_name="步骤描述")
    expected_result = models.TextField(blank=True, verbose_name="预期结果")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending", verbose_name="状态")
    error_message = models.TextField(blank=True, verbose_name="错误信息")
    metrics = models.JSONField(default=dict, blank=True, verbose_name="性能指标")
    screenshot_before = models.ImageField(upload_to="uiauto/screenshots/", null=True, blank=True, verbose_name="执行前截图")
    screenshot_after = models.ImageField(upload_to="uiauto/screenshots/", null=True, blank=True, verbose_name="执行后截图")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "UI自动化步骤记录"
        verbose_name_plural = "UI自动化步骤记录"
        ordering = ["step_number", "id"]

