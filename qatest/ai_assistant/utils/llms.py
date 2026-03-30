from django.conf import settings
from users.ai_config import AIKeyNotConfigured, resolve_testcase_params

try:
    from autogen_ext.models.openai import OpenAIChatCompletionClient
except Exception:
    OpenAIChatCompletionClient = None

def _setup_openai_compatible_client(model: str, api_key: str, base_url: str, vision: bool):
    if OpenAIChatCompletionClient is None:
        raise RuntimeError("缺少依赖 autogen-ext：请安装后再启用 AI 功能")
    if not api_key:
        raise RuntimeError("未配置 API Key")
    model_config = {
        "model": model,
        "api_key": api_key,
        "model_info": {
            "vision": vision,
            "function_calling": True,
            "json_output": True,
            "family": "unknown",
            "multiple_system_messages": True,
            "structured_output": True,
        },
        "base_url": base_url,
    }
    return OpenAIChatCompletionClient(**model_config)


def _setup_vllm_model_client():
    """设置 Qwen-VL 模型客户端"""
    if not getattr(settings, "AI_QWEN_API_KEY", ""):
        raise RuntimeError("未配置 AI_QWEN_API_KEY：请在环境变量或项目根目录 .env 中设置")
    return _setup_openai_compatible_client(
        model=settings.AI_QWEN_MODEL,
        api_key=settings.AI_QWEN_API_KEY,
        base_url=settings.AI_QWEN_BASE_URL,
        vision=True,
    )

_model_client_cache = None
_model_client_cache_key = None
_text_model_client_cache = None
_text_model_client_cache_key = None
_review_model_client_cache = None
_review_model_client_cache_key = None
_user_client_cache = {}


def get_model_client(user=None):
    global _model_client_cache, _model_client_cache_key
    if user is None:
        raise AIKeyNotConfigured("请先在个人中心配置用例生成 API Key", scope="testcase")
    params = resolve_testcase_params(user)
    cache_key = ("user_default",) + params.cache_key() + (True,)
    cached = _user_client_cache.get(cache_key)
    if cached is not None:
        return cached
    client = _setup_openai_compatible_client(
        model=params.model,
        api_key=params.api_key,
        base_url=params.base_url,
        vision=True,
    )
    _user_client_cache[cache_key] = client
    return client


def get_text_model_client(user=None):
    global _text_model_client_cache, _text_model_client_cache_key
    if user is None:
        raise AIKeyNotConfigured("请先在个人中心配置用例生成 API Key", scope="testcase")
    params = resolve_testcase_params(user)
    cache_key = ("user_text",) + params.cache_key() + (False,)
    cached = _user_client_cache.get(cache_key)
    if cached is not None:
        return cached
    client = _setup_openai_compatible_client(
        model=params.model,
        api_key=params.api_key,
        base_url=params.base_url,
        vision=False,
    )
    _user_client_cache[cache_key] = client
    return client


def get_vision_model_client(user=None):
    return get_model_client(user=user)


def get_review_model_client(user=None):
    global _review_model_client_cache, _review_model_client_cache_key
    if user is None:
        raise AIKeyNotConfigured("请先在个人中心配置用例生成 API Key", scope="testcase")
    params = resolve_testcase_params(user)
    key = ("user_review",) + params.cache_key() + (False,)
    if _review_model_client_cache is None or _review_model_client_cache_key != key:
        _review_model_client_cache = _setup_openai_compatible_client(
            model=params.model,
            api_key=params.api_key,
            base_url=params.base_url,
            vision=False,
        )
        _review_model_client_cache_key = key
    return _review_model_client_cache
