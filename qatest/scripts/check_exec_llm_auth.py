import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "qa_platform.settings")

import django  # noqa: E402

django.setup()  # noqa: E402

from openai import APIStatusError, AsyncOpenAI  # noqa: E402

from users.ai_config import resolve_exec_params  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402


def _load_params():
    user_id = os.environ.get("USER_ID", "").strip()
    if user_id.isdigit():
        user = get_user_model().objects.filter(id=int(user_id)).first()
    else:
        user = get_user_model().objects.order_by("-id").first()
    if user is None:
        return None, None
    return user, resolve_exec_params(user)


async def _probe_async(model: str, api_key: str, base_url: str) -> None:
    client = AsyncOpenAI(api_key=api_key, base_url=(base_url.rstrip("/") + "/"))
    try:
        r = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            temperature=0,
        )
        txt = (r.choices[0].message.content or "").strip()
        print("ok", True)
        print("reply_prefix", txt[:80])
    except APIStatusError as e:
        print("ok", False)
        print("status_code", e.status_code)
        msg = (e.message or "").strip()
        print("message_prefix", msg[:200])
    except Exception as e:
        print("ok", False)
        print("error", type(e).__name__, str(e)[:200])


if __name__ == "__main__":
    user, p = _load_params()
    if user is None or p is None:
        print("no_user")
        raise SystemExit(0)
    print("user_id", getattr(user, "id", None))
    print("provider", p.provider)
    print("model", p.model)
    print("base_url", p.base_url)
    print("has_key", bool(p.api_key))
    asyncio.run(_probe_async(p.model, p.api_key, p.base_url))
