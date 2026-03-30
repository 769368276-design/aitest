import json


IMPORTANT_KEYWORDS = [
    "login",
    "auth",
    "token",
    "user",
    "current",
    "session",
    "oauth",
    "refresh",
    "graphql",
]

IMPORTANT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _is_important(method: str, url: str) -> bool:
    m = (method or "").upper()
    u = (url or "").lower()
    if m in IMPORTANT_METHODS:
        return True
    return any(k in u for k in IMPORTANT_KEYWORDS)


def _parse_json_maybe(text: str):
    if not text:
        return None
    s = str(text).strip()
    if not s.startswith("{") and not s.startswith("["):
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _find_token(obj) -> bool:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() == "token" and v:
                return True
            if _find_token(v):
                return True
    if isinstance(obj, list):
        return any(_find_token(x) for x in obj)
    return False


def evaluate_execution_assertions(network_rows: list[dict]) -> list[dict]:
    important = [r for r in network_rows if _is_important(r.get("method", ""), r.get("url", ""))]

    failed_important = [
        r for r in important if not (200 <= int(r.get("status_code") or 0) < 300)
    ]

    failed_codes = []
    for r in failed_important:
        try:
            failed_codes.append(int(r.get("status_code") or 0))
        except Exception:
            failed_codes.append(0)
    failed_codes = sorted(set(failed_codes))
    failed_codes_text = ", ".join(str(x) for x in failed_codes[:10]) if failed_codes else ""

    assertions = []
    assertions.append(
        {
            "name": "关键接口返回码为 200-299",
            "passed": len(important) > 0 and len(failed_important) == 0,
            "detail": (
                f"关键接口 {len(important)} 条，失败 {len(failed_important)} 条"
                + (f"（失败状态码：{failed_codes_text}）" if failed_codes_text else "")
            ),
        }
    )

    login_candidates = [
        r
        for r in network_rows
        if (r.get("method") or "").upper() == "POST"
        and "login" in (r.get("url") or "").lower()
    ]
    login_ok = False
    for r in login_candidates:
        payload = _parse_json_maybe(r.get("response_data") or "")
        if isinstance(payload, dict) and "body_json" in payload:
            if _find_token(payload.get("body_json")):
                login_ok = True
                break
        if payload and _find_token(payload):
            login_ok = True
            break

    assertions.append(
        {
            "name": "登录接口返回 token（如存在）",
            "passed": (len(login_candidates) == 0) or login_ok,
            "detail": f"匹配到登录请求 {len(login_candidates)} 条",
        }
    )

    user_candidates = [
        r for r in network_rows if "user/current" in (r.get("url") or "").lower()
    ]
    user_ok = any(200 <= int(r.get("status_code") or 0) < 300 for r in user_candidates)
    assertions.append(
        {
            "name": "用户信息接口返回 200-299（如存在）",
            "passed": (len(user_candidates) == 0) or user_ok,
            "detail": f"匹配到用户信息请求 {len(user_candidates)} 条",
        }
    )

    return assertions
