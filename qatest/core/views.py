import os
import time
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone

from bugs.models import Bug
from users.ai_config import AIKeyNotConfigured, resolve_exec_params, resolve_testcase_params, resolve_ocr_params


def _ensure_playwright_browsers_path():
    if (os.getenv("PLAYWRIGHT_BROWSERS_PATH", "") or "").strip():
        return
    try:
        base_dir = getattr(settings, "BASE_DIR", None)
        if not base_dir:
            return
        pw = os.path.join(str(base_dir), ".playwright")
        if os.path.isdir(pw):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = pw
    except Exception:
        return

@login_required
def index(request):
    # Simple stats for P1 Workbench
    my_projects = request.user.projectmember_set.all()
    # If user is owner
    owned_projects = request.user.owned_projects.all()
    
    pending_bugs = Bug.objects.filter(assignee=request.user, status__in=[1, 2, 3, 7])
    
    context = {
        'my_projects_count': my_projects.count() + owned_projects.count(),
        'pending_bugs_count': pending_bugs.count(),
    }
    return render(request, 'core/index.html', context)


@login_required
def diagnostics(request):
    _ensure_playwright_browsers_path()
    user = request.user
    ai = {"exec": None, "testcase": None, "ocr": None}
    for k, fn in (("exec", resolve_exec_params), ("testcase", resolve_testcase_params), ("ocr", resolve_ocr_params)):
        try:
            p = fn(user)
            ai[k] = {
                "ok": True,
                "provider": getattr(p, "provider", ""),
                "model": getattr(p, "model", ""),
                "base_url": getattr(p, "base_url", ""),
            }
        except AIKeyNotConfigured as e:
            ai[k] = {"ok": False, "error": str(e) or "未配置", "scope": getattr(e, "scope", "")}
        except Exception as e:
            ai[k] = {"ok": False, "error": str(e) or "未知错误", "scope": ""}

    browser = {
        "ai_exec_headless": bool(getattr(settings, "AI_EXEC_HEADLESS", True)),
        "ai_exec_chrome_path": (os.getenv("AI_EXEC_CHROME_PATH", "") or "").strip(),
        "playwright_browsers_path": (os.getenv("PLAYWRIGHT_BROWSERS_PATH", "") or "").strip(),
    }
    try:
        from playwright.sync_api import sync_playwright
        browser["playwright_ok"] = True
        browser["chromium_executable_path"] = ""
    except Exception as e:
        browser["playwright_ok"] = False
        browser["chromium_executable_path"] = ""
        browser["playwright_probe_error"] = str(e)

    media = {
        "debug": bool(getattr(settings, "DEBUG", False)),
        "serve_media": bool(getattr(settings, "SERVE_MEDIA", False)),
        "media_url": getattr(settings, "MEDIA_URL", ""),
        "media_root": str(getattr(settings, "MEDIA_ROOT", "")),
    }

    migrations = {"pending": None, "error": ""}
    try:
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        migrations["pending"] = bool(plan)
    except Exception as e:
        migrations["pending"] = None
        migrations["error"] = str(e)[:400]

    last_media_url = request.session.get("diagnostics_last_media_url")
    last_browser_check = request.session.get("diagnostics_last_browser_check")
    return render(
        request,
        "core/diagnostics.html",
        {
            "ai": ai,
            "browser": browser,
            "media": media,
            "migrations": migrations,
            "last_media_url": last_media_url,
            "last_browser_check": last_browser_check,
        },
    )


@login_required
def diagnostics_browser_check(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "method_not_allowed"}, status=405)

    started = time.time()
    out = {"ok": False, "detail": "", "elapsed_ms": 0}
    try:
        _ensure_playwright_browsers_path()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            used_pw = (os.getenv("PLAYWRIGHT_BROWSERS_PATH", "") or "").strip()
            exe = ""
            try:
                exe = getattr(p.chromium, "executable_path", "") or ""
            except Exception:
                exe = ""
            headless = bool(getattr(settings, "AI_EXEC_HEADLESS", True))
            b = p.chromium.launch(
                headless=headless,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
            )
            b.close()
        out["ok"] = True
        out["detail"] = f"ok\nPLAYWRIGHT_BROWSERS_PATH={used_pw}\nchromium_executable_path={exe}"
    except Exception as e:
        out["ok"] = False
        out["detail"] = str(e)[:800]
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    request.session["diagnostics_last_browser_check"] = out
    try:
        request.session.modified = True
    except Exception:
        pass
    if out["ok"]:
        messages.success(request, "浏览器自检成功（chromium headless 可启动）")
    else:
        messages.error(request, f"浏览器自检失败：{out['detail']}")
    return redirect("diagnostics")


@login_required
def diagnostics_media_check(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "method_not_allowed"}, status=405)

    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\xf8\x0f\x00\x01\x01\x01\x00\x18\xdd\x8d\x18\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    ts = timezone.now().strftime("%Y%m%d-%H%M%S")
    name = f"diagnostics/diag-{ts}-{uuid.uuid4().hex}.png"
    try:
        default_storage.save(name, ContentFile(png))
        url = (getattr(settings, "MEDIA_URL", "/media/") or "/media/").rstrip("/") + "/" + name
        request.session["diagnostics_last_media_url"] = url
        request.session.modified = True
        messages.success(request, f"已生成测试图片：{url}")
    except Exception as e:
        messages.error(request, f"生成测试图片失败：{e}")
    return redirect("diagnostics")
