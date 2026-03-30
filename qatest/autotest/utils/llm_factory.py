import os
from browser_use.llm.openai.chat import ChatOpenAI
from browser_use.llm.anthropic.chat import ChatAnthropic
from browser_use.llm.google.chat import ChatGoogle
from browser_use.llm.ollama.chat import ChatOllama
from django.conf import settings
from users.ai_config import AIKeyNotConfigured, resolve_exec_params
from autotest.utils.openai_compat_chat import ChatOpenAICompat

def get_llm_model(provider: str = "openai", model_name: str = "gpt-4o", temperature: float = 0.0, user=None):
    """
    Factory to create browser-use compatible LLM instances.
    """
    if user is None:
        raise AIKeyNotConfigured("请先在个人中心配置 AI执行 的 API Key", scope="exec")
    p = resolve_exec_params(user)
    provider = str(getattr(p, "provider", "") or provider)
    model_name = str(getattr(p, "model", "") or model_name)
    api_key = str(getattr(p, "api_key", "") or "")
    base_url = str(getattr(p, "base_url", "") or "")
    provider_l = provider.strip().lower()
    base_l = base_url.strip().lower()
    model_l = model_name.strip().lower()
    if provider_l == "kimi" or ("moonshot" in base_l) or model_l.startswith("kimi-"):
        temperature = 1.0
    if provider == "anthropic":
        return ChatAnthropic(model=model_name, temperature=temperature, api_key=api_key)
    if provider == "google":
        return ChatGoogle(model=model_name, temperature=temperature, api_key=api_key)
    if provider == "ollama":
        return ChatOllama(model=model_name, temperature=temperature, base_url=base_url)
    openai_compat = provider_l in {"openai", "kimi", "qwen"} or bool(base_l)
    if openai_compat:
        return ChatOpenAICompat(model=model_name, temperature=temperature, api_key=api_key, base_url=base_url)
    return ChatOpenAI(model=model_name, temperature=temperature, api_key=api_key, base_url=base_url)
    
