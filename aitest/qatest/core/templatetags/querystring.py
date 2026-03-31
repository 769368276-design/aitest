from __future__ import annotations

from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def querystring(context, **kwargs) -> str:
    request = context.get("request")
    if not request:
        return ""
    q = request.GET.copy()
    for k, v in (kwargs or {}).items():
        if v is None or v == "":
            try:
                q.pop(k, None)
            except Exception:
                pass
        else:
            q[str(k)] = str(v)
    return q.urlencode()

