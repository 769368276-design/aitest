from django.db import models
from django.conf import settings
from projects.models import Project

class Requirement(models.Model):
    PRIORITY_CHOICES = (
        (1, '高'),
        (2, '中'),
        (3, '低')
    )
    STATUS_CHOICES = (
        (1, '待评审'),
        (2, '已确认'),
        (3, '开发中'),
        (4, '已完成'),
        (5, '已关闭')
    )
    TYPE_CHOICES = (
        (1, '功能需求'),
        (2, '非功能需求'),
        (3, '优化需求')
    )

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='requirements', verbose_name="所属项目")
    title = models.CharField(max_length=200, verbose_name="需求名称")
    description = models.TextField(verbose_name="需求描述")
    type = models.IntegerField(choices=TYPE_CHOICES, default=1, verbose_name="类型")
    priority = models.IntegerField(choices=PRIORITY_CHOICES, default=2, verbose_name="优先级")
    status = models.IntegerField(choices=STATUS_CHOICES, default=1, verbose_name="状态")
    expected_finish_time = models.DateTimeField(null=True, blank=True, verbose_name="预计完成时间")
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='created_requirements', verbose_name="创建人")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    version = models.IntegerField(default=1, verbose_name="版本号")

    class Meta:
        verbose_name = "需求"
        verbose_name_plural = "需求"

    def __str__(self):
        return self.title
