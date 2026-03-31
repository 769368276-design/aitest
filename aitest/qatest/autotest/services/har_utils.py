import json
from datetime import datetime, timezone


def build_har(entries: list[dict]) -> dict:
    started = datetime.now(timezone.utc).isoformat()
    har_entries = []

    for e in entries:
        request_payload = e.get("request_payload") or {}
        response_payload = e.get("response_payload") or {}
        url = e.get("url") or request_payload.get("url") or ""
        method = (e.get("method") or "GET").upper()
        status = int(e.get("status_code") or response_payload.get("status") or 0)

        req_headers = []
        for k, v in (request_payload.get("headers") or {}).items():
            req_headers.append({"name": str(k), "value": str(v)})

        res_headers = []
        for k, v in (response_payload.get("headers") or {}).items():
            res_headers.append({"name": str(k), "value": str(v)})

        post_data_text = ""
        if "body_json" in request_payload:
            try:
                post_data_text = json.dumps(request_payload["body_json"], ensure_ascii=False)
            except Exception:
                post_data_text = str(request_payload["body_json"])
        elif "body_form" in request_payload:
            try:
                post_data_text = json.dumps(request_payload["body_form"], ensure_ascii=False)
            except Exception:
                post_data_text = str(request_payload["body_form"])
        elif "body_raw" in request_payload:
            post_data_text = str(request_payload["body_raw"])

        mime_type = ""
        for hk, hv in (response_payload.get("headers") or {}).items():
            if str(hk).lower() == "content-type":
                mime_type = str(hv)
                break
        if not mime_type:
            mime_type = "application/json" if "body_json" in response_payload else "text/plain"

        content_text = ""
        if "body_json" in response_payload:
            try:
                content_text = json.dumps(response_payload["body_json"], ensure_ascii=False)
            except Exception:
                content_text = str(response_payload["body_json"])
        elif "body_text" in response_payload:
            content_text = str(response_payload["body_text"])

        duration_ms = None
        for hk, hv in (response_payload.get("headers") or {}).items():
            if str(hk).lower() == "x_duration_ms":
                try:
                    duration_ms = int(hv)
                except Exception:
                    duration_ms = None
                break

        har_entries.append(
            {
                "startedDateTime": started,
                "time": duration_ms if duration_ms is not None else 0,
                "request": {
                    "method": method,
                    "url": url,
                    "httpVersion": "HTTP/1.1",
                    "headers": req_headers,
                    "queryString": [],
                    "cookies": [],
                    "headersSize": -1,
                    "bodySize": len(post_data_text.encode("utf-8")) if post_data_text else 0,
                    "postData": {
                        "mimeType": "application/json",
                        "text": post_data_text,
                    }
                    if post_data_text
                    else None,
                },
                "response": {
                    "status": status,
                    "statusText": "",
                    "httpVersion": "HTTP/1.1",
                    "headers": res_headers,
                    "cookies": [],
                    "content": {
                        "size": len(content_text.encode("utf-8")) if content_text else 0,
                        "mimeType": mime_type,
                        "text": content_text,
                    },
                    "redirectURL": "",
                    "headersSize": -1,
                    "bodySize": len(content_text.encode("utf-8")) if content_text else 0,
                },
                "cache": {},
                "timings": {"send": 0, "wait": duration_ms if duration_ms is not None else 0, "receive": 0},
            }
        )

    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "QA Platform", "version": "1.0"},
            "pages": [],
            "entries": har_entries,
        }
    }

