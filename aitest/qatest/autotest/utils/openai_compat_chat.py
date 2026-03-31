import json
from dataclasses import dataclass
from typing import Any, TypeVar, overload

import httpx
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel

from browser_use.llm.base import BaseChatModel
from browser_use.llm.exceptions import ModelProviderError, ModelRateLimitError
from browser_use.llm.messages import BaseMessage
from browser_use.llm.openai.serializer import OpenAIMessageSerializer
from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage

T = TypeVar("T", bound=BaseModel)


def _extract_first_json_value(text: str) -> Any:
    s = (text or "").strip()
    if not s:
        raise ValueError("empty")
    if s.startswith("```"):
        if s.endswith("```"):
            s = s[:-3].strip()
        s = s.strip("`").strip()
        if "\n" in s:
            s = s.split("\n", 1)[1].strip()
    start = -1
    for i, ch in enumerate(s):
        if ch in ("{", "["):
            start = i
            break
    if start < 0:
        raise ValueError("no_json_start")
    cand = s[start:]
    dec = json.JSONDecoder()
    obj, _end = dec.raw_decode(cand)
    return obj


def _normalize_usage(usage: Any) -> ChatInvokeUsage | None:
    try:
        if usage is None:
            return None

        def _get(key: str) -> Any:
            if isinstance(usage, dict):
                return usage.get(key)
            return getattr(usage, key, None)

        prompt_tokens = _get("prompt_tokens")
        completion_tokens = _get("completion_tokens")
        total_tokens = _get("total_tokens")
        if prompt_tokens is None or completion_tokens is None or total_tokens is None:
            return None

        prompt_cached_tokens = _get("prompt_cached_tokens")
        prompt_cache_creation_tokens = _get("prompt_cache_creation_tokens")
        prompt_image_tokens = _get("prompt_image_tokens")

        return ChatInvokeUsage(
            prompt_tokens=int(prompt_tokens),
            prompt_cached_tokens=int(prompt_cached_tokens) if prompt_cached_tokens is not None else None,
            prompt_cache_creation_tokens=int(prompt_cache_creation_tokens)
            if prompt_cache_creation_tokens is not None
            else None,
            prompt_image_tokens=int(prompt_image_tokens) if prompt_image_tokens is not None else None,
            completion_tokens=int(completion_tokens),
            total_tokens=int(total_tokens),
        )
    except Exception:
        return None


def _normalize_agent_output_obj(obj: Any) -> Any:
    try:
        if not isinstance(obj, dict):
            return obj
        actions = obj.get("action")
        if not isinstance(actions, list):
            return obj

        def _as_int(v: Any) -> int | None:
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.strip().isdigit():
                return int(v.strip())
            return None

        def _as_key(v: Any) -> str:
            if isinstance(v, str):
                return v.strip()
            if isinstance(v, (list, tuple)):
                for x in v:
                    s = _as_key(x)
                    if s:
                        return s
                return ""
            if isinstance(v, (int, float)):
                return str(v)
            return ""

        def _normalize_action(action: Any) -> Any:
            if not isinstance(action, dict) or len(action) != 1:
                return action
            action_name = next(iter(action.keys()))
            params = action.get(action_name)

            if action_name in ("click_element_by_index", "click_element"):
                action_name = "click"
                action = {action_name: params}
            elif action_name in ("input_text", "type_text", "type", "fill"):
                action_name = "input"
                action = {action_name: params}
            elif action_name in ("upload", "upload_file_by_index"):
                action_name = "upload_file"
                action = {action_name: params}

            if action_name in ("send_keys", "press_key", "press"):
                key = ""
                if isinstance(params, dict):
                    for k in ("keys", "key", "text", "value", "input"):
                        if k in params:
                            key = _as_key(params.get(k))
                            if key:
                                break
                else:
                    key = _as_key(params)
                if key:
                    return {"send_keys": {"keys": key}}
                return action

            if action_name == "wait":
                if isinstance(params, dict):
                    if "time" in params and "seconds" not in params:
                        params["seconds"] = params.pop("time")
                    if "duration" in params and "seconds" not in params:
                        params["seconds"] = params.pop("duration")
                    if "timeout" in params and "seconds" not in params:
                        params["seconds"] = params.pop("timeout")
                    if "timeout_ms" in params and "seconds" not in params:
                        try:
                            params["seconds"] = max(0, float(params.pop("timeout_ms")) / 1000.0)
                        except Exception:
                            params.pop("timeout_ms", None)
                    if "milliseconds" in params and "seconds" not in params:
                        try:
                            params["seconds"] = max(0, float(params.pop("milliseconds")) / 1000.0)
                        except Exception:
                            params.pop("milliseconds", None)
                elif isinstance(params, (int, float)):
                    action[action_name] = {"seconds": params}
                return action

            if isinstance(params, dict):
                for noise in ("timeout", "timeout_ms", "wait", "wait_ms", "delay", "delay_ms", "sleep", "sleep_ms"):
                    params.pop(noise, None)
                if "index" not in params and "element" in params:
                    idx = _as_int(params.get("element"))
                    if idx is not None:
                        params["index"] = idx
                        params.pop("element", None)
                if "index" not in params and "element_id" in params:
                    idx = _as_int(params.get("element_id"))
                    if idx is not None:
                        params["index"] = idx
                        params.pop("element_id", None)
                if "index" not in params and "element_index" in params:
                    idx = _as_int(params.get("element_index"))
                    if idx is not None:
                        params["index"] = idx
                        params.pop("element_index", None)
                if "index" not in params and "element_idx" in params:
                    idx = _as_int(params.get("element_idx"))
                    if idx is not None:
                        params["index"] = idx
                        params.pop("element_idx", None)
                if action_name == "input":
                    if "text" not in params:
                        txt = params.get("value", params.get("input", params.get("content", "")))
                        if txt is not None:
                            params["text"] = str(txt)
                    params.pop("value", None)
                    params.pop("input", None)
                    params.pop("content", None)
                if action_name == "upload_file":
                    if "path" not in params and "file" in params:
                        params["path"] = str(params.get("file") or "")
                    params.pop("file", None)
                action[action_name] = params
            return action

        obj["action"] = [_normalize_action(a) for a in actions]
        return obj
    except Exception:
        return obj


@dataclass
class ChatOpenAICompat(BaseChatModel):
    model: str
    api_key: str | None = None
    base_url: str | httpx.URL | None = None
    timeout: float | httpx.Timeout | None = None
    max_retries: int = 5
    temperature: float | None = 1.0

    @property
    def provider(self) -> str:
        return "openai"

    @property
    def name(self) -> str:
        return str(self.model)

    def get_client(self) -> AsyncOpenAI:
        timeout = self.timeout
        if timeout is None:
            timeout = httpx.Timeout(120.0, connect=20.0)
        client_params = {
            "api_key": self.api_key,
            "base_url": self.base_url,
            "timeout": timeout,
            "max_retries": self.max_retries,
        }
        client_params = {k: v for k, v in client_params.items() if v is not None}
        return AsyncOpenAI(**client_params)

    @overload
    async def ainvoke(self, messages: list[BaseMessage], output_format: None = None, **kwargs: Any) -> ChatInvokeCompletion[str]: ...

    @overload
    async def ainvoke(self, messages: list[BaseMessage], output_format: type[T], **kwargs: Any) -> ChatInvokeCompletion[T]: ...

    async def ainvoke(
        self, messages: list[BaseMessage], output_format: type[T] | None = None, **kwargs: Any
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
        openai_messages = OpenAIMessageSerializer.serialize_messages(messages)
        try:
            model_params: dict[str, Any] = {}
            if self.temperature is not None:
                model_params["temperature"] = float(self.temperature)
            response = await self.get_client().chat.completions.create(
                model=self.model,
                messages=openai_messages,
                **model_params,
            )
            content = response.choices[0].message.content or ""
            usage = _normalize_usage(getattr(response, "usage", None))

            if output_format is None:
                return ChatInvokeCompletion(
                    completion=content,
                    usage=usage,  # type: ignore[arg-type]
                    stop_reason=response.choices[0].finish_reason if response.choices else None,
                )

            obj = _extract_first_json_value(content)
            try:
                parsed = output_format.model_validate(obj)
            except Exception:
                parsed = output_format.model_validate(_normalize_agent_output_obj(obj))
            return ChatInvokeCompletion(
                completion=parsed,
                usage=usage,  # type: ignore[arg-type]
                stop_reason=response.choices[0].finish_reason if response.choices else None,
            )
        except RateLimitError as e:
            raise ModelRateLimitError(message=e.message, model=self.name) from e
        except APIConnectionError as e:
            raise ModelProviderError(message=str(e), model=self.name) from e
        except APIStatusError as e:
            raise ModelProviderError(message=e.message, status_code=e.status_code, model=self.name) from e
        except Exception as e:
            raise ModelProviderError(message=str(e), model=self.name) from e
