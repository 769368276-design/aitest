import base64
import json
from typing import Any, Dict, Optional

import requests
from django.conf import settings


def _normalize_base_url(base_url: str) -> str:
    u = (base_url or "").strip()
    if not u:
        return ""
    return u.rstrip("/")


def _chat_completions_url(base_url: str) -> str:
    u = _normalize_base_url(base_url)
    if u.endswith("/v1"):
        return u + "/chat/completions"
    return u + "/v1/chat/completions"


def qwen_ocr_image_bytes(
    image_bytes: bytes,
    mime_type: str = "image/png",
    model: Optional[str] = None,
    timeout_s: int = 60,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    api_key = (api_key or "").strip()
    base_url = (base_url or "").strip()
    model_name = (model or getattr(settings, "AI_QWEN_OCR_MODEL", "") or getattr(settings, "AI_QWEN_MODEL", "") or "").strip()
    if not (api_key and base_url and model_name):
        raise RuntimeError("请先在个人中心配置 OCR 的 Base URL / API Key / 模型名称")

    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime_type};base64,{b64}"

    prompt = (
        "你是OCR引擎，只允许识别图片中的文字，不要推断、不要补全、不要解释。\n"
        "请输出 JSON（不要使用```包裹），结构如下：\n"
        "{\n"
        '  "text": "识别出的全文（尽量保持原行序）",\n'
        '  "confidence": "high|medium|low",\n'
        '  "warnings": ["如果存在不清晰/无法识别的区域，简要说明；否则空数组"]\n'
        "}\n"
        "如果无法识别到任何文字，text 置为空字符串，并给出 warnings。"
    )

    body = {
        "model": model_name,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }

    url = _chat_completions_url(base_url)
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        data=json.dumps(body, ensure_ascii=False),
        timeout=timeout_s,
    )
    resp.raise_for_status()
    data = resp.json()
    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    if not content:
        return {"text": "", "confidence": "low", "warnings": ["empty_response"]}

    try:
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            content = content[start : end + 1]
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            parsed.setdefault("text", "")
            parsed.setdefault("confidence", "medium")
            parsed.setdefault("warnings", [])
            return parsed
    except Exception:
        pass
    return {"text": content, "confidence": "medium", "warnings": ["non_json_response"]}

