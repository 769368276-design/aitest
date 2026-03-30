from django.contrib.auth.models import AbstractUser, Group
from django.db import models
from projects.models import Project

class User(AbstractUser):
    phone = models.CharField(max_length=20, blank=True, null=True)
    # System level role. A user might have different roles in different projects.
    # This role defines global permissions (e.g. create project, system admin).
    role = models.ForeignKey(Group, on_delete=models.SET_NULL, null=True, blank=True, related_name='users')
    
    class Meta:
        verbose_name = 'User'
        verbose_name_plural = 'Users'

    def __str__(self):
        return self.username or self.email


class UserGroup(models.Model):
    name = models.CharField(max_length=80, verbose_name="用户组名称")
    owner = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="owned_user_groups", verbose_name="创建人")
    shared_projects = models.ManyToManyField(Project, blank=True, related_name="shared_user_groups", verbose_name="共享项目")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        verbose_name = "用户组"
        verbose_name_plural = "用户组"
        unique_together = ("owner", "name")

    def __str__(self):
        return self.name


class UserGroupMember(models.Model):
    ROLE_CHOICES = (
        ("owner", "Owner"),
        ("admin", "Admin"),
        ("member", "Member"),
    )
    group = models.ForeignKey(UserGroup, on_delete=models.CASCADE, related_name="members", verbose_name="用户组")
    user = models.ForeignKey("users.User", on_delete=models.CASCADE, related_name="user_group_memberships", verbose_name="用户")
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default="member", verbose_name="组内角色")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="加入时间")

    class Meta:
        verbose_name = "用户组成员"
        verbose_name_plural = "用户组成员"
        unique_together = ("group", "user")


class UserAIModelConfig(models.Model):
    PROVIDER_CHOICES = (
        ("qwen", "Qwen（DashScope/OpenAI兼容）"),
        ("kimi", "Kimi（Moonshot/OpenAI兼容）"),
        ("minimax", "MiniMax（OpenAI兼容）"),
        ("doubao", "豆包（火山引擎/OpenAI兼容）"),
        ("glm", "GLM（智谱/OpenAI兼容）"),
        ("openai", "OpenAI"),
        ("deepseek", "DeepSeek（OpenAI兼容）"),
        ("openrouter", "OpenRouter（OpenAI兼容）"),
        ("openai_compatible", "自定义 OpenAI 兼容"),
        ("anthropic", "Anthropic（Claude）"),
        ("google", "Google（Gemini）"),
        ("ollama", "Ollama（本地）"),
    )

    user = models.OneToOneField("users.User", on_delete=models.CASCADE, related_name="ai_model_config", verbose_name="用户")

    testcase_provider = models.CharField(max_length=32, choices=PROVIDER_CHOICES, default="qwen", verbose_name="用例生成-模型提供方")
    testcase_model = models.CharField(max_length=120, blank=True, verbose_name="用例生成-模型名称")
    testcase_api_key = models.TextField(blank=True, verbose_name="用例生成-API Key")
    testcase_base_url = models.CharField(max_length=255, blank=True, verbose_name="用例生成-Base URL")

    exec_provider = models.CharField(max_length=32, choices=PROVIDER_CHOICES, default="qwen", verbose_name="AI执行-模型提供方")
    exec_model = models.CharField(max_length=120, blank=True, verbose_name="AI执行-模型名称")
    exec_api_key = models.TextField(blank=True, verbose_name="AI执行-API Key")
    exec_base_url = models.CharField(max_length=255, blank=True, verbose_name="AI执行-Base URL")

    ocr_provider = models.CharField(max_length=32, choices=PROVIDER_CHOICES, default="qwen", verbose_name="OCR-模型提供方")
    ocr_model = models.CharField(max_length=120, blank=True, verbose_name="OCR-模型名称")
    ocr_api_key = models.TextField(blank=True, verbose_name="OCR-API Key")
    ocr_base_url = models.CharField(max_length=255, blank=True, verbose_name="OCR-Base URL")

    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        verbose_name = "用户AI模型配置"
        verbose_name_plural = "用户AI模型配置"


class UserCICredential(models.Model):
    user = models.OneToOneField("users.User", on_delete=models.CASCADE, related_name="ci_credential", verbose_name="用户")
    token = models.CharField(max_length=80, unique=True, verbose_name="Token")
    enabled = models.BooleanField(default=True, verbose_name="启用")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    last_used_at = models.DateTimeField(blank=True, null=True, verbose_name="最近使用时间")

    class Meta:
        verbose_name = "CI/CD 凭证"
        verbose_name_plural = "CI/CD 凭证"

    def __str__(self):
        return f"{self.user_id}:{'enabled' if self.enabled else 'disabled'}"
