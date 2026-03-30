import uuid
import datetime
from django.db import models
from django.conf import settings
from testcases.models import TestCase, TestCaseStep
from projects.models import Project

class AutoTestExecution(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('paused', 'Paused'),
        ('stopped', 'Stopped'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    )

    case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name='auto_executions', verbose_name="测试用例")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="状态")
    executor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, verbose_name="执行人")
    start_time = models.DateTimeField(auto_now_add=True, verbose_name="开始时间")
    end_time = models.DateTimeField(null=True, blank=True, verbose_name="结束时间")
    # Store JSON report summary
    result_summary = models.JSONField(default=dict, blank=True, verbose_name="结果汇总")

    har_file = models.FileField(upload_to='autotest/har/', null=True, blank=True, verbose_name="HAR文件")
    
    # Control flags
    stop_signal = models.BooleanField(default=False, verbose_name="停止信号")
    pause_signal = models.BooleanField(default=False, verbose_name="暂停信号")
    batch_id = models.UUIDField(null=True, blank=True, verbose_name="批次ID")
    run_index = models.PositiveIntegerField(default=1, verbose_name="轮次序号")
    run_total = models.PositiveIntegerField(default=1, verbose_name="轮次总数")
    dataset_name = models.CharField(max_length=120, blank=True, verbose_name="数据集名称")
    dataset_vars = models.JSONField(default=dict, blank=True, verbose_name="数据集变量")
    trigger_source = models.CharField(max_length=30, blank=True, default="", verbose_name="触发来源")
    trigger_payload = models.JSONField(default=dict, blank=True, verbose_name="触发参数")
    schedule = models.ForeignKey("AutoTestSchedule", on_delete=models.SET_NULL, null=True, blank=True, related_name="executions", verbose_name="计划任务")

    class Meta:
        verbose_name = "自动化执行记录"
        verbose_name_plural = "自动化执行记录"
        ordering = ['-start_time']

    def __str__(self):
        return f"{self.case.title} - {self.status}"

class AutoTestStepRecord(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('success', 'Success'),
        ('failed', 'Failed'),
        ('skipped', 'Skipped'),
    )

    execution = models.ForeignKey(AutoTestExecution, on_delete=models.CASCADE, related_name='step_records', verbose_name="执行记录")
    step = models.ForeignKey(TestCaseStep, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="关联步骤")
    step_number = models.IntegerField(verbose_name="步骤序号")
    description = models.TextField(verbose_name="步骤描述")
    
    # AI Decision & Playwright Action
    ai_thought = models.TextField(blank=True, verbose_name="AI思考")
    action_script = models.TextField(blank=True, verbose_name="执行脚本")
    
    # Execution Result
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name="状态")
    error_message = models.TextField(blank=True, verbose_name="错误信息")

    metrics = models.JSONField(default=dict, blank=True, verbose_name="性能指标")
    
    # Screenshots
    screenshot_before = models.ImageField(upload_to='autotest/screenshots/', null=True, blank=True, verbose_name="执行前截图")
    screenshot_after = models.ImageField(upload_to='autotest/screenshots/', null=True, blank=True, verbose_name="执行后截图")
    ocr_screenshot = models.ImageField(upload_to='autotest/screenshots/', null=True, blank=True, verbose_name="OCR截图")
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "自动化步骤记录"
        verbose_name_plural = "自动化步骤记录"
        ordering = ['step_number']

class AutoTestNetworkEntry(models.Model):
    step_record = models.ForeignKey(AutoTestStepRecord, on_delete=models.CASCADE, related_name='network_entries', verbose_name="步骤记录")
    url = models.TextField(verbose_name="URL")
    method = models.CharField(max_length=10, verbose_name="Method")
    status_code = models.IntegerField(verbose_name="Status Code")
    request_data = models.TextField(blank=True, verbose_name="请求数据")
    response_data = models.TextField(blank=True, verbose_name="响应数据")
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "网络请求日志"
        verbose_name_plural = "网络请求日志"


class AutoTestReportShare(models.Model):
    execution = models.OneToOneField(
        AutoTestExecution,
        on_delete=models.CASCADE,
        related_name="share",
        verbose_name="执行记录",
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, verbose_name="分享Token")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="创建人",
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "执行报告分享"
        verbose_name_plural = "执行报告分享"


class AutoTestSchedule(models.Model):
    TYPE_CHOICES = (
        ("interval", "按间隔"),
        ("daily", "每天定点"),
        ("once", "单次"),
    )

    project = models.ForeignKey(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name="autotest_schedules", verbose_name="项目")
    name = models.CharField(max_length=120, verbose_name="计划名称")
    enabled = models.BooleanField(default=True, verbose_name="启用")

    schedule_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default="interval", verbose_name="计划类型")
    interval_minutes = models.PositiveIntegerField(default=60, verbose_name="间隔(分钟)")
    daily_time = models.TimeField(null=True, blank=True, verbose_name="每天时间")
    run_at = models.DateTimeField(null=True, blank=True, verbose_name="执行时间(单次)")

    case_ids = models.JSONField(default=list, blank=True, verbose_name="用例ID列表")

    next_run_at = models.DateTimeField(null=True, blank=True, verbose_name="下次执行时间")
    last_run_at = models.DateTimeField(null=True, blank=True, verbose_name="上次执行时间")
    locked_until = models.DateTimeField(null=True, blank=True, verbose_name="锁定到期")
    last_status = models.CharField(max_length=30, blank=True, default="", verbose_name="上次状态")
    last_error = models.TextField(blank=True, default="", verbose_name="上次错误")

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, verbose_name="创建人")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "自动化计划任务"
        verbose_name_plural = "自动化计划任务"
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.name}"

    def compute_next_run_at(self, now: datetime.datetime) -> datetime.datetime | None:
        if self.schedule_type == "once":
            return None
        if self.schedule_type == "daily":
            t = self.daily_time or datetime.time(2, 0, 0)
            base = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
            if base <= now:
                base = base + datetime.timedelta(days=1)
            return base
        mins = int(self.interval_minutes or 60)
        if mins < 1:
            mins = 1
        return now + datetime.timedelta(minutes=mins)
