from __future__ import annotations

import hashlib
import os
import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.utils import OperationalError
import logging


@dataclass(frozen=True)
class OpenAICompatibleParams:
    provider: str
    model: str
    api_key: str
    base_url: str

    def cache_key(self) -> tuple:
        h = hashlib.sha256((self.api_key or "").encode("utf-8")).hexdigest() if self.api_key else ""
        return (self.provider, self.model, self.base_url, h)


@dataclass(frozen=True)
class ExecLLMParams:
    provider: str
    model: str
    api_key: str
    base_url: str = ""


class AIKeyNotConfigured(RuntimeError):
    def __init__(self, message: str, scope: str = ""):
        super().__init__(message)
        self.scope = scope



def _norm_base_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    u = u.strip("`").strip().strip('"').strip("'").strip()
    if "10.255.255.1" in u:
        return ""
    return u

logger = logging.getLogger(__name__)

def _get_user_ai_model_config_model():
    try:
        return apps.get_model("users", "UserAIModelConfig")
    except Exception:
        return None

def _coerce_user(user):
    if user is None:
        return None
    if getattr(user, "is_authenticated", None) is not None:
        return user
    try:
        uid = int(user)
    except Exception:
        return None
    try:
        return get_user_model().objects.get(pk=uid)
    except Exception:
        return None


def _get_or_create_cfg(user) -> Optional[object]:
    user = _coerce_user(user)
    UserAIModelConfig = _get_user_ai_model_config_model()
    if UserAIModelConfig is None or user is None or not getattr(user, "is_authenticated", False):
        return None

    try:
        cached = getattr(getattr(user, "_state", None), "fields_cache", {}).get("ai_model_config")
        if cached is not None:
            try:
                cached.refresh_from_db()
            except Exception:
                pass
            return cached
    except Exception:
        pass

    try:
        asyncio.get_running_loop()
        return None
    except Exception:
        pass

    last_err = None
    for attempt in range(3):
        try:
            cfg = UserAIModelConfig.objects.filter(user_id=getattr(user, "id", None)).first()
            if cfg is not None:
                return cfg
            cfg, _ = UserAIModelConfig.objects.get_or_create(user=user)
            return cfg
        except OperationalError as e:
            last_err = e
            msg = str(e).lower()
            if "database is locked" in msg or "locked" in msg:
                time.sleep(0.15 * (attempt + 1))
                continue
            break
        except Exception as e:
            last_err = e
            break
    try:
        logger.error("load UserAIModelConfig failed: user_id=%s err=%s", getattr(user, "id", None), last_err)
    except Exception:
        pass
    return None


def resolve_testcase_params(user) -> OpenAICompatibleParams:
    try:
        pre = getattr(user, "_ai_testcase_params", None)
        if isinstance(pre, OpenAICompatibleParams):
            return pre
    except Exception:
        pass
    cfg = _get_or_create_cfg(user)
    if cfg is None:
        raise AIKeyNotConfigured("请先登录后再使用 AI 功能", scope="testcase")
    provider = str(getattr(cfg, "testcase_provider", "") or "qwen").strip().lower() if cfg else "qwen"
    model = str(getattr(cfg, "testcase_model", "") or "").strip()
    api_key = str(getattr(cfg, "testcase_api_key", "") or "").strip()
    base_url = str(getattr(cfg, "testcase_base_url", "") or "").strip()
    return _resolve_provider_defaults(provider, model, api_key, base_url, purpose="testcase", strict_key=True)


def resolve_exec_params(user) -> ExecLLMParams:
    try:
        pre = getattr(user, "_ai_exec_params", None)
        if isinstance(pre, ExecLLMParams):
            return pre
    except Exception:
        pass
    cfg = _get_or_create_cfg(user)
    if cfg is None:
        raise AIKeyNotConfigured("请先登录后再使用 AI 功能", scope="exec")
    provider = str(getattr(cfg, "exec_provider", "") or "qwen").strip().lower() if cfg else "qwen"
    model = str(getattr(cfg, "exec_model", "") or "").strip()
    api_key = str(getattr(cfg, "exec_api_key", "") or "").strip()
    base_url = str(getattr(cfg, "exec_base_url", "") or "").strip()
    if not api_key:
        provider = str(getattr(cfg, "testcase_provider", "") or provider).strip().lower()
        model = str(getattr(cfg, "testcase_model", "") or model).strip()
        api_key = str(getattr(cfg, "testcase_api_key", "") or "").strip()
        base_url = str(getattr(cfg, "testcase_base_url", "") or base_url).strip()
    return _resolve_exec_provider_defaults(provider, model, api_key, base_url, strict_key=True)


def resolve_ocr_params(user) -> OpenAICompatibleParams:
    try:
        pre = getattr(user, "_ai_ocr_params", None)
        if isinstance(pre, OpenAICompatibleParams):
            return pre
    except Exception:
        pass
    cfg = _get_or_create_cfg(user)
    if cfg is None:
        raise AIKeyNotConfigured("请先登录后再使用 AI 功能", scope="ocr")
    provider = str(getattr(cfg, "ocr_provider", "") or "qwen").strip().lower() if cfg else "qwen"
    model = str(getattr(cfg, "ocr_model", "") or "").strip()
    api_key = str(getattr(cfg, "ocr_api_key", "") or "").strip()
    base_url = str(getattr(cfg, "ocr_base_url", "") or "").strip()
    return _resolve_provider_defaults(provider, model, api_key, base_url, purpose="ocr", strict_key=True)


def _resolve_exec_provider_defaults(provider: str, model: str, api_key: str, base_url: str, strict_key: bool) -> ExecLLMParams:
    provider = (provider or "").strip().lower() or "qwen"
    base_url = _norm_base_url(base_url)

    if provider == "anthropic":
        if strict_key and not api_key:
            raise AIKeyNotConfigured("请先在个人中心配置 AI执行 的 API Key", scope="exec")
        model = model or "claude-3-5-sonnet-20241022"
        return ExecLLMParams(provider=provider, model=model, api_key=api_key, base_url="")

    if provider == "google":
        if strict_key and not api_key:
            raise AIKeyNotConfigured("请先在个人中心配置 AI执行 的 API Key", scope="exec")
        model = model or "gemini-1.5-pro"
        return ExecLLMParams(provider=provider, model=model, api_key=api_key, base_url="")

    if provider == "ollama":
        base_url = base_url or "http://localhost:11434"
        model = model or "llava"
        if strict_key:
            if not model or not base_url:
                raise AIKeyNotConfigured("请先在个人中心配置 AI执行 的模型名称与 Base URL", scope="exec")
        return ExecLLMParams(provider=provider, model=model, api_key=(api_key or "ollama"), base_url=_norm_base_url(base_url))

    if provider == "openai":
        if strict_key and not api_key:
            raise AIKeyNotConfigured("请先在个人中心配置 AI执行 的 API Key", scope="exec")
        base_url = base_url or "https://api.openai.com/v1"
        model = model or "gpt-4o"
        return ExecLLMParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "deepseek":
        if strict_key and not api_key:
            raise AIKeyNotConfigured("请先在个人中心配置 AI执行 的 API Key", scope="exec")
        base_url = base_url or "https://api.deepseek.com"
        model = model or "deepseek-chat"
        return ExecLLMParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "openrouter":
        if strict_key and not api_key:
            raise AIKeyNotConfigured("请先在个人中心配置 AI执行 的 API Key", scope="exec")
        base_url = base_url or "https://openrouter.ai/api/v1"
        model = model or "openai/gpt-4o"
        return ExecLLMParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "kimi":
        if strict_key and not api_key:
            raise AIKeyNotConfigured("请先在个人中心配置 AI执行 的 API Key", scope="exec")
        if base_url and (("dashscope" in base_url) or ("aliyuncs.com" in base_url)):
            base_url = ""
        base_url = base_url or "https://api.moonshot.cn/v1"
        model = model or "kimi-k2.5"
        return ExecLLMParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "minimax":
        if strict_key and not api_key:
            raise AIKeyNotConfigured("请先在个人中心配置 AI执行 的 API Key", scope="exec")
        base_url = base_url or "https://api.minimax.chat/v1"
        model = model or "MiniMax-Text-01"
        return ExecLLMParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "doubao":
        if strict_key and not api_key:
            raise AIKeyNotConfigured("请先在个人中心配置 AI执行 的 API Key", scope="exec")
        base_url = base_url or "https://ark.cn-beijing.volces.com/api/v3"
        model = model or "doubao-1.5-vision-pro"
        return ExecLLMParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "glm":
        if strict_key and not api_key:
            raise AIKeyNotConfigured("请先在个人中心配置 AI执行 的 API Key", scope="exec")
        base_url = base_url or "https://open.bigmodel.cn/api/paas/v4/"
        model = model or "glm-4v"
        return ExecLLMParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider in ("qwen", "openai_compatible"):
        if strict_key and not api_key:
            raise AIKeyNotConfigured("请先在个人中心配置 AI执行 的 API Key", scope="exec")
        if base_url and (("moonshot" in base_url) or ("api.moonshot" in base_url)):
            base_url = ""
        base_url = base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = model or getattr(settings, "AI_QWEN_MODEL", "") or "qwen-vl-plus"
        return ExecLLMParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if strict_key and not api_key:
        raise AIKeyNotConfigured("请先在个人中心配置 AI执行 的 API Key", scope="exec")
    if strict_key and (not model or not base_url):
        raise AIKeyNotConfigured("请先在个人中心配置 AI执行 的模型名称与 Base URL", scope="exec")
    return ExecLLMParams(provider="openai_compatible", model=model, api_key=api_key, base_url=_norm_base_url(base_url))


def _resolve_provider_defaults(provider: str, model: str, api_key: str, base_url: str, purpose: str, strict_key: bool) -> OpenAICompatibleParams:
    provider = (provider or "").strip().lower() or "qwen"
    base_url = _norm_base_url(base_url)

    if provider == "openai":
        if strict_key and not api_key:
            raise AIKeyNotConfigured(f"请先在个人中心配置 {('用例生成' if purpose=='testcase' else 'OCR')} 的 API Key", scope=purpose)
        base_url = base_url or "https://api.openai.com/v1"
        model = model or "gpt-4o"
        return OpenAICompatibleParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "deepseek":
        if strict_key and not api_key:
            raise AIKeyNotConfigured(f"请先在个人中心配置 {('用例生成' if purpose=='testcase' else 'OCR')} 的 API Key", scope=purpose)
        base_url = base_url or "https://api.deepseek.com"
        model = model or "deepseek-chat"
        return OpenAICompatibleParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "openrouter":
        if strict_key and not api_key:
            raise AIKeyNotConfigured(f"请先在个人中心配置 {('用例生成' if purpose=='testcase' else 'OCR')} 的 API Key", scope=purpose)
        base_url = base_url or "https://openrouter.ai/api/v1"
        model = model or "openai/gpt-4o"
        return OpenAICompatibleParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "kimi":
        if strict_key and not api_key:
            raise AIKeyNotConfigured(f"请先在个人中心配置 {('用例生成' if purpose=='testcase' else 'OCR')} 的 API Key", scope=purpose)
        if base_url and (("dashscope" in base_url) or ("aliyuncs.com" in base_url)):
            base_url = ""
        base_url = base_url or "https://api.moonshot.cn/v1"
        model = model or "kimi-k2.5"
        return OpenAICompatibleParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "minimax":
        if strict_key and not api_key:
            raise AIKeyNotConfigured(f"请先在个人中心配置 {('用例生成' if purpose=='testcase' else 'OCR')} 的 API Key", scope=purpose)
        base_url = base_url or "https://api.minimax.chat/v1"
        model = model or "MiniMax-Text-01"
        return OpenAICompatibleParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "doubao":
        if strict_key and not api_key:
            raise AIKeyNotConfigured(f"请先在个人中心配置 {('用例生成' if purpose=='testcase' else 'OCR')} 的 API Key", scope=purpose)
        base_url = base_url or "https://ark.cn-beijing.volces.com/api/v3"
        model = model or "doubao-1.5-vision-pro"
        return OpenAICompatibleParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "glm":
        if strict_key and not api_key:
            raise AIKeyNotConfigured(f"请先在个人中心配置 {('用例生成' if purpose=='testcase' else 'OCR')} 的 API Key", scope=purpose)
        base_url = base_url or "https://open.bigmodel.cn/api/paas/v4/"
        model = model or ("glm-4v" if purpose == "ocr" else "glm-4.7")
        return OpenAICompatibleParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "ollama":
        base_url = base_url or "http://localhost:11434/v1"
        model = model or "llava"
        api_key = api_key or "ollama"
        if strict_key:
            if not model or not base_url:
                raise AIKeyNotConfigured(f"请先在个人中心配置 {('用例生成' if purpose=='testcase' else 'OCR')} 的模型名称与 Base URL", scope=purpose)
        return OpenAICompatibleParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "qwen":
        if strict_key and not api_key:
            raise AIKeyNotConfigured(f"请先在个人中心配置 {('用例生成' if purpose=='testcase' else 'OCR')} 的 API Key", scope=purpose)
        if base_url and (("moonshot" in base_url) or ("api.moonshot" in base_url)):
            base_url = ""
        base_url = base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = model or getattr(settings, "AI_QWEN_MODEL", "") or "qwen-vl-plus"
        return OpenAICompatibleParams(provider=provider, model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider in ("anthropic", "google"):
        if strict_key:
            raise AIKeyNotConfigured(f"{('用例生成' if purpose=='testcase' else 'OCR')} 暂不支持直连 {provider}，请改用 OpenRouter 或 自定义OpenAI兼容", scope=purpose)
        return OpenAICompatibleParams(provider="openai_compatible", model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if provider == "openai_compatible":
        if strict_key and not api_key:
            raise AIKeyNotConfigured(f"请先在个人中心配置 {('用例生成' if purpose=='testcase' else 'OCR')} 的 API Key", scope=purpose)
        if strict_key and (not model or not base_url):
            raise AIKeyNotConfigured(f"请先在个人中心配置 {('用例生成' if purpose=='testcase' else 'OCR')} 的模型名称与 Base URL", scope=purpose)
        return OpenAICompatibleParams(provider="openai_compatible", model=model, api_key=api_key, base_url=_norm_base_url(base_url))

    if strict_key:
        raise AIKeyNotConfigured(f"请先在个人中心配置 {('用例生成' if purpose=='testcase' else 'OCR')} 的模型与 Key", scope=purpose)
    return OpenAICompatibleParams(provider="openai_compatible", model=model, api_key=api_key, base_url=_norm_base_url(base_url))
