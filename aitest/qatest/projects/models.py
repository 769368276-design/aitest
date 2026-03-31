from django.db import models
from django.conf import settings
from django.contrib.auth.models import Group

class Project(models.Model):
    STATUS_CHOICES = (
        (1, '待启动'),
        (2, '进行中'),
        (3, '已暂停'),
        (4, '已结束'),
    )
    name = models.CharField(max_length=100, verbose_name="项目名称")
    description = models.TextField(blank=True, verbose_name="项目描述")
    base_url = models.URLField(blank=True, default="", verbose_name="项目URL")
    test_accounts = models.TextField(blank=True, default="", verbose_name="测试账号")
    history_requirements = models.TextField(blank=True, default="", verbose_name="历史需求")
    knowledge_base = models.TextField(blank=True, default="", verbose_name="项目资料库")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='owned_projects', verbose_name="负责人")
    status = models.IntegerField(choices=STATUS_CHOICES, default=1, verbose_name="状态")
    start_time = models.DateTimeField(null=True, blank=True, verbose_name="开始时间")
    end_time = models.DateTimeField(null=True, blank=True, verbose_name="结束时间")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "项目"
        verbose_name_plural = "项目"

    def __str__(self):
        return self.name

class ProjectMember(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='members', verbose_name="项目")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, verbose_name="用户")
    role = models.ForeignKey(Group, on_delete=models.SET_NULL, null=True, related_name='project_memberships', verbose_name="角色") 
    join_time = models.DateTimeField(auto_now_add=True, verbose_name="加入时间")

    class Meta:
        unique_together = ('project', 'user')
        verbose_name = "项目成员"
        verbose_name_plural = "项目成员"
