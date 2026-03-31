from django.db import models
from django.conf import settings
from projects.models import Project
from requirements.models import Requirement

class TestCase(models.Model):
    PRIORITY_CHOICES = ((1, '高'), (2, '中'), (3, '低'))
    TYPE_CHOICES = ((1, '功能用例'), (2, '性能用例'), (3, '兼容性用例'))
    EXEC_STATUS_CHOICES = (
        (0, '未执行'),
        (1, '执行中'),
        (2, '通过'),
        (3, '失败'),
        (4, '阻塞'),
        (5, '已执行'),
    )
    
    EXEC_TYPE_CHOICES = (
        (1, '人工执行'),
        (2, 'AI 自动化'),
    )

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='testcases', verbose_name="所属项目")
    requirement = models.ForeignKey(Requirement, on_delete=models.SET_NULL, null=True, blank=True, related_name='testcases', verbose_name="关联需求")
    title = models.CharField(max_length=200, verbose_name="用例标题")
    pre_condition = models.TextField(blank=True, verbose_name="前置条件")
    # Removed text fields for steps and expected_result
    type = models.IntegerField(choices=TYPE_CHOICES, default=1, verbose_name="类型")
    execution_type = models.IntegerField(choices=EXEC_TYPE_CHOICES, default=1, verbose_name="执行方式")
    priority = models.IntegerField(choices=PRIORITY_CHOICES, default=2, verbose_name="优先级")
    status = models.IntegerField(choices=EXEC_STATUS_CHOICES, default=0, verbose_name="执行状态")
    
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='created_cases', verbose_name="创建人")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    case_mode = models.CharField(
        max_length=20,
        default="normal",
        choices=(("normal", "普通用例"), ("advanced", "高级用例")),
        verbose_name="用例模式",
    )
    parameters = models.JSONField(default=dict, blank=True, verbose_name="参数集")

    class Meta:
        verbose_name = "测试用例"
        verbose_name_plural = "测试用例"

    def __str__(self):
        return self.title

class TestCaseStep(models.Model):
    case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name='steps', verbose_name="所属用例")
    step_number = models.PositiveIntegerField(verbose_name="步骤序号")
    description = models.TextField(verbose_name="步骤描述")
    expected_result = models.TextField(verbose_name="预期结果")
    smart_data_enabled = models.BooleanField(default=False, verbose_name="智能数据生成")
    guide_image = models.ImageField(upload_to="testcase_step_guides/%Y/%m/%d/", null=True, blank=True, verbose_name="参考截图")
    guide_image_base64 = models.TextField(null=True, blank=True, verbose_name="参考截图(Base64)")
    guide_image_content_type = models.CharField(max_length=120, blank=True, default="", verbose_name="参考截图类型")
    transfer_file_name = models.CharField(max_length=255, blank=True, default="", verbose_name="传输文件名")
    transfer_file_content_type = models.CharField(max_length=120, blank=True, default="", verbose_name="传输文件类型")
    transfer_file_size = models.IntegerField(default=0, verbose_name="传输文件大小(字节)")
    transfer_file_base64 = models.TextField(null=True, blank=True, verbose_name="传输文件(Base64)")
    # If users want to mark individual steps as passed/failed
    # But usually status is for the whole case. 
    # Requirement: "打勾选择执行和未执行" -> seems like a simple boolean or status per step
    is_executed = models.BooleanField(default=False, verbose_name="已执行")

    class Meta:
        ordering = ['step_number']
        verbose_name = "测试步骤"
        verbose_name_plural = "测试步骤"

    def save(self, *args, **kwargs):
        old = None
        if self.pk:
            try:
                old = TestCaseStep.objects.get(pk=self.pk)
            except Exception:
                old = None
        super().save(*args, **kwargs)
        try:
            if old and old.guide_image and old.guide_image.name:
                if not self.guide_image or (self.guide_image.name != old.guide_image.name):
                    try:
                        old.guide_image.delete(save=False)
                    except Exception:
                        pass
        except Exception:
            pass

    def delete(self, *args, **kwargs):
        try:
            if self.guide_image and self.guide_image.name:
                try:
                    self.guide_image.delete(save=False)
                except Exception:
                    pass
        except Exception:
            pass
        return super().delete(*args, **kwargs)

class CaseExecution(models.Model):
    case = models.ForeignKey(TestCase, on_delete=models.CASCADE, related_name='executions', verbose_name="用例")
    status = models.IntegerField(choices=TestCase.EXEC_STATUS_CHOICES, verbose_name="执行状态")
    executor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, verbose_name="执行人")
    execute_time = models.DateTimeField(auto_now_add=True, verbose_name="执行时间")
    remark = models.TextField(blank=True, verbose_name="备注")
    bug = models.ForeignKey('bugs.Bug', on_delete=models.SET_NULL, null=True, blank=True, related_name='executions', verbose_name="关联Bug")

    class Meta:
        verbose_name = "执行记录"
        verbose_name_plural = "执行记录"
