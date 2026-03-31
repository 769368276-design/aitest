from django.db import models
from django.conf import settings
from projects.models import Project

class Bug(models.Model):
    SEVERITY_CHOICES = ((1, '致命'), (2, '严重'), (3, '一般'), (4, '轻微'))
    PRIORITY_CHOICES = ((1, '高'), (2, '中'), (3, '低'))
    STATUS_CHOICES = (
        (1, '新建'),
        (2, '已分配'),
        (3, '开发中'),
        (4, '已修复'),
        (5, '待验证'),
        (6, '已关闭'),
        (7, '重开')
    )
    
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='bugs', verbose_name="所属项目")
    case = models.ForeignKey('testcases.TestCase', on_delete=models.SET_NULL, null=True, blank=True, related_name='bugs', verbose_name="关联用例")
    
    title = models.CharField(max_length=200, verbose_name="Bug标题")
    description = models.TextField(verbose_name="问题描述")
    reproduce_steps = models.TextField(verbose_name="复现步骤")
    severity = models.IntegerField(choices=SEVERITY_CHOICES, default=3, verbose_name="严重程度")
    priority = models.IntegerField(choices=PRIORITY_CHOICES, default=2, verbose_name="优先级")
    status = models.IntegerField(choices=STATUS_CHOICES, default=1, verbose_name="状态")
    
    affected_version = models.CharField(max_length=50, blank=True, verbose_name="影响版本")
    fixed_version = models.CharField(max_length=50, blank=True, verbose_name="修复版本")
    
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='created_bugs', verbose_name="创建人")
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='assigned_bugs', verbose_name="指派给")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "缺陷"
        verbose_name_plural = "缺陷"

    def __str__(self):
        return self.title

class BugAttachment(models.Model):
    bug = models.ForeignKey(Bug, on_delete=models.CASCADE, related_name='attachments', verbose_name="所属Bug")
    file = models.FileField(upload_to='bug_attachments/', verbose_name="文件")
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name="上传时间")

    class Meta:
        verbose_name = "Bug附件"
        verbose_name_plural = "Bug附件"
