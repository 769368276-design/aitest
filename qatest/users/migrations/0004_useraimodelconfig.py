from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0003_usergroup_shared_projects"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserAIModelConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("testcase_provider", models.CharField(choices=[("qwen", "Qwen（DashScope/OpenAI兼容）"), ("openai", "OpenAI"), ("deepseek", "DeepSeek（OpenAI兼容）"), ("openrouter", "OpenRouter（OpenAI兼容）"), ("openai_compatible", "自定义 OpenAI 兼容"), ("anthropic", "Anthropic（Claude）"), ("google", "Google（Gemini）"), ("ollama", "Ollama（本地）")], default="qwen", max_length=32, verbose_name="用例生成-模型提供方")),
                ("testcase_model", models.CharField(blank=True, max_length=120, verbose_name="用例生成-模型名称")),
                ("testcase_api_key", models.TextField(blank=True, verbose_name="用例生成-API Key")),
                ("testcase_base_url", models.CharField(blank=True, max_length=255, verbose_name="用例生成-Base URL")),
                ("exec_provider", models.CharField(choices=[("qwen", "Qwen（DashScope/OpenAI兼容）"), ("openai", "OpenAI"), ("deepseek", "DeepSeek（OpenAI兼容）"), ("openrouter", "OpenRouter（OpenAI兼容）"), ("openai_compatible", "自定义 OpenAI 兼容"), ("anthropic", "Anthropic（Claude）"), ("google", "Google（Gemini）"), ("ollama", "Ollama（本地）")], default="qwen", max_length=32, verbose_name="AI执行-模型提供方")),
                ("exec_model", models.CharField(blank=True, max_length=120, verbose_name="AI执行-模型名称")),
                ("exec_api_key", models.TextField(blank=True, verbose_name="AI执行-API Key")),
                ("exec_base_url", models.CharField(blank=True, max_length=255, verbose_name="AI执行-Base URL")),
                ("ocr_provider", models.CharField(choices=[("qwen", "Qwen（DashScope/OpenAI兼容）"), ("openai", "OpenAI"), ("deepseek", "DeepSeek（OpenAI兼容）"), ("openrouter", "OpenRouter（OpenAI兼容）"), ("openai_compatible", "自定义 OpenAI 兼容"), ("anthropic", "Anthropic（Claude）"), ("google", "Google（Gemini）"), ("ollama", "Ollama（本地）")], default="qwen", max_length=32, verbose_name="OCR-模型提供方")),
                ("ocr_model", models.CharField(blank=True, max_length=120, verbose_name="OCR-模型名称")),
                ("ocr_api_key", models.TextField(blank=True, verbose_name="OCR-API Key")),
                ("ocr_base_url", models.CharField(blank=True, max_length=255, verbose_name="OCR-Base URL")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="ai_model_config", to=settings.AUTH_USER_MODEL, verbose_name="用户")),
            ],
            options={
                "verbose_name": "用户AI模型配置",
                "verbose_name_plural": "用户AI模型配置",
            },
        ),
    ]

