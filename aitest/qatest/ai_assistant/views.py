from django.shortcuts import render
from django.http import StreamingHttpResponse, JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
import os
import asyncio
import json
import threading
import uuid
import re
import time
from .services.ai_service import ai_service
from asgiref.sync import sync_to_async, async_to_sync
from projects.models import Project
from requirements.models import Requirement
from testcases.models import TestCase, TestCaseStep
from django.conf import settings
from core.visibility import visible_projects
from ai_assistant.services.export_service import generate_xlsx_bytes, generate_xmind_bytes, build_export_filename
from ai_assistant.services.testcase_generation.postprocess import dedup_cases
from ai_assistant.services.testcase_generation.postprocess import parse_cases_from_markdown
from ai_assistant.models import AIGenerationJob
from users.ai_config import AIKeyNotConfigured, resolve_ocr_params, resolve_testcase_params

def _enrich_generation_inputs(project, req_obj, context: str, requirements: str):
    context = (context or "").strip()
    requirements = (requirements or "").strip()
    if context in ("1", "无", "无。", "暂无", "暂无。"):
        context = ""
    if requirements in ("1", "无", "无。", "暂无", "暂无。"):
        requirements = ""
    if project:
        parts = []
        parts.append(f"项目名称：{project.name}")
        if getattr(project, "base_url", ""):
            parts.append(f"项目URL：{project.base_url}")
        if getattr(project, "test_accounts", ""):
            parts.append("测试账号：\n" + str(project.test_accounts).strip())
        if getattr(project, "history_requirements", ""):
            parts.append("历史需求：\n" + str(project.history_requirements).strip())
        if getattr(project, "knowledge_base", ""):
            parts.append("项目资料库：\n" + str(project.knowledge_base).strip())
        parts.append("注意：项目资料用于补充提升准确性，但不要因此减少用例数量与覆盖范围。")

        try:
            recent_cases = (
                TestCase.objects.filter(project=project)
                .order_by("-created_at")
                .prefetch_related("steps")[:10]
            )
            if recent_cases:
                lines = []
                for c in recent_cases:
                    lines.append(f"- #{c.id} {c.title[:120]}")
                parts.append("项目关联用例标题（最近10条，仅供风格参考，不要照抄）：\n" + "\n".join(lines))
        except Exception:
            pass

        kb = "\n\n".join([p for p in parts if p and str(p).strip()])
        if kb:
            kb = kb[:3500]
            context = (context + "\n\n" if context else "") + "【项目资料】\n" + kb

    if req_obj:
        req_block = "\n".join([
            f"需求标题：{req_obj.title}",
            f"需求类型：{req_obj.get_type_display()}",
            f"优先级：{req_obj.get_priority_display()}",
            f"状态：{req_obj.get_status_display()}",
            "需求描述：\n" + str(req_obj.description or "").strip(),
        ])
        req_block = req_block[:4000]
        requirements = (requirements + "\n\n" if requirements else "") + "【关联需求】\n" + req_block
    return context, requirements

@login_required
def index(request):
    projects = visible_projects(request.user)
    requirements = Requirement.objects.filter(project__in=projects)
    return render(request, 'ai_assistant/index.html', {'projects': projects, 'requirements': requirements})

_generation_cancel_events = {}
_generation_cancel_lock = threading.Lock()


def _get_or_create_cancel_event(generation_id: str) -> threading.Event:
    with _generation_cancel_lock:
        ev = _generation_cancel_events.get(generation_id)
        if ev is None:
            ev = threading.Event()
            _generation_cancel_events[generation_id] = ev
        return ev


def _pop_cancel_event(generation_id: str) -> None:
    with _generation_cancel_lock:
        _generation_cancel_events.pop(generation_id, None)


async def stream_generator(file_path, context, requirements, cancel_event: threading.Event, user=None):
    try:
        yield json.dumps({"type": "meta", "message": "start"}, ensure_ascii=False) + "\n"
        engine_mode = (getattr(settings, "AI_TESTCASE_ENGINE", "legacy") or "legacy").strip().lower()
        if engine_mode != "legacy":
            from ai_assistant.services.testcase_generation import TestCaseGenerationEngine
            engine = TestCaseGenerationEngine()
            saw_done = False
            async for ev in engine.generate(file_path, context, requirements, cancel_event=cancel_event, user=user):
                payload = {"type": ev.type}
                if getattr(ev, "text", ""):
                    payload["text"] = ev.text
                if getattr(ev, "message", ""):
                    payload["message"] = ev.message
                if getattr(ev, "code", ""):
                    payload["code"] = ev.code
                if getattr(ev, "page", None) is not None:
                    payload["page"] = ev.page
                yield json.dumps(payload, ensure_ascii=False) + "\n"
                if ev.type == "done":
                    saw_done = True
            if not saw_done:
                yield json.dumps({"type": "done", "message": "done"}, ensure_ascii=False) + "\n"
        else:
            async for chunk in ai_service.generate_test_cases_stream(file_path, context, requirements, cancel_event=cancel_event, user=user):
                if chunk:
                    yield json.dumps({"type": "delta", "text": chunk}, ensure_ascii=False) + "\n"
            yield json.dumps({"type": "done", "message": "done"}, ensure_ascii=False) + "\n"
    except Exception as e:
        yield json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False) + "\n"
    finally:
        # Cleanup file after generation
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass

        try:
            cancel_event.set()
        except Exception:
            pass

@csrf_exempt
def generate(request):
    """
    Sync wrapper for async generation to ensure compatibility with WSGI server
    """
    if request.method == 'POST':
        if not request.user.is_authenticated:
            return StreamingHttpResponse("Unauthorized", status=401)
            
        uploaded_file = request.FILES.get('file')
        context = request.POST.get('context', '')
        requirements = request.POST.get('requirements', '')
        project_id = request.POST.get('project') or request.POST.get('project_id')
        requirement_id = request.POST.get('requirement') or request.POST.get('requirement_id')
        
        if not uploaded_file:
             return StreamingHttpResponse("请上传文件", status=400)

        ext = ""
        try:
            ext = (str(getattr(uploaded_file, "name", "") or "").rsplit(".", 1)[-1] or "").lower()
        except Exception:
            ext = ""
        try:
            tc_params = resolve_testcase_params(request.user)
            try:
                setattr(request.user, "_ai_testcase_params", tc_params)
            except Exception:
                pass
            mode = str(getattr(settings, "AI_PDF_PAGEWISE_MODE", "balanced") or "balanced").strip().lower()
            enable_pdf_ocr = bool(getattr(settings, "AI_PDF_PAGEWISE_OCR", False)) and mode != "fast"
            if ext == "pdf" and enable_pdf_ocr:
                ocr_params = resolve_ocr_params(request.user)
                try:
                    setattr(request.user, "_ai_ocr_params", ocr_params)
                except Exception:
                    pass
        except AIKeyNotConfigured as e:
            msg = str(e) or "请先在个人中心配置 API Key"
            def _gen():
                yield json.dumps({"type": "error", "message": msg}, ensure_ascii=False) + "\n"
            return StreamingHttpResponse(_gen(), content_type='application/x-ndjson; charset=utf-8', status=200)

        project = None
        if project_id and str(project_id).isdigit():
            try:
                project = Project.objects.get(pk=int(project_id), id__in=visible_projects(request.user).values_list("id", flat=True))
            except Project.DoesNotExist:
                project = None

        req_obj = None
        if project and requirement_id and str(requirement_id).isdigit():
            try:
                req_obj = Requirement.objects.get(pk=int(requirement_id), project=project)
            except Requirement.DoesNotExist:
                req_obj = None

        context, requirements = _enrich_generation_inputs(project, req_obj, context, requirements)

        # Ensure temp directory exists
        temp_dir = os.path.join(settings.MEDIA_ROOT, 'temp')
        os.makedirs(temp_dir, exist_ok=True)
        
        file_path = os.path.join(temp_dir, uploaded_file.name)
        with open(file_path, 'wb+') as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)
        
        if not os.path.isabs(file_path):
            file_path = os.path.abspath(file_path)

        generation_id = (request.headers.get("X-Generation-Id") or "").strip()
        if not generation_id:
            generation_id = str(uuid.uuid4())
        cancel_event = _get_or_create_cancel_event(generation_id)

        return StreamingHttpResponse(
            sync_stream_generator(file_path, context, requirements, generation_id, cancel_event, user=request.user),
            content_type='application/x-ndjson; charset=utf-8'
        )
    return StreamingHttpResponse("仅支持POST请求", status=405)

@csrf_exempt
def stop_generation(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "仅支持POST请求"}, status=405)
    if not request.user.is_authenticated:
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=401)
    try:
        data = json.loads(request.body or "{}")
    except Exception:
        data = {}
    generation_id = str(data.get("generation_id") or "").strip()
    if not generation_id:
        return JsonResponse({"success": False, "error": "generation_id不能为空"}, status=400)
    ev = _get_or_create_cancel_event(generation_id)
    ev.set()
    return JsonResponse({"success": True})


_job_cancel_events = {}
_job_cancel_lock = threading.Lock()
_job_threads = {}
_job_threads_lock = threading.Lock()


def _get_or_create_job_cancel_event(job_id: int) -> threading.Event:
    with _job_cancel_lock:
        ev = _job_cancel_events.get(int(job_id))
        if ev is None:
            ev = threading.Event()
            _job_cancel_events[int(job_id)] = ev
        return ev


def _pop_job_cancel_event(job_id: int) -> None:
    with _job_cancel_lock:
        _job_cancel_events.pop(int(job_id), None)


def _set_job_thread(job_id: int, t: threading.Thread) -> None:
    with _job_threads_lock:
        _job_threads[int(job_id)] = t


def _get_job_thread(job_id: int) -> threading.Thread | None:
    with _job_threads_lock:
        return _job_threads.get(int(job_id))


def _pop_job_thread(job_id: int) -> None:
    with _job_threads_lock:
        _job_threads.pop(int(job_id), None)


async def _job_update_text_async(job_id: int, text: str, progress: str = "") -> None:
    if not text and not progress:
        return
    try:
        job = await sync_to_async(AIGenerationJob.objects.get)(id=int(job_id))
    except Exception:
        return
    try:
        if text:
            job.markdown_text = (job.markdown_text or "") + str(text)
        if progress:
            job.progress_message = str(progress)[:255]
        await sync_to_async(job.save)(update_fields=["markdown_text", "progress_message", "updated_at"])
    except Exception:
        try:
            await sync_to_async(job.save)()
        except Exception:
            pass


async def _job_set_status_async(job_id: int, status: str, error: str = "") -> None:
    try:
        job = await sync_to_async(AIGenerationJob.objects.get)(id=int(job_id))
    except Exception:
        return
    try:
        job.status = str(status or "")[:20]
        if error:
            job.error_message = str(error)[:4000]
        await sync_to_async(job.save)(update_fields=["status", "error_message", "updated_at"])
    except Exception:
        try:
            await sync_to_async(job.save)()
        except Exception:
            pass


async def _job_finalize_cases_async(job_id: int) -> None:
    try:
        job = await sync_to_async(AIGenerationJob.objects.get)(id=int(job_id))
    except Exception:
        return
    try:
        text = str(job.markdown_text or "")
        cases = parse_cases_from_markdown(text)
        cases = dedup_cases(cases)
        job.cases_json = cases
        await sync_to_async(job.save)(update_fields=["cases_json", "updated_at"])
    except Exception:
        pass


def _run_job_thread(job_id: int, file_path: str, context: str, requirements: str, cancel_event: threading.Event, user) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def _run():
            engine_mode = (getattr(settings, "AI_TESTCASE_ENGINE", "legacy") or "legacy").strip().lower()
            max_seconds = int(getattr(settings, "AI_TESTCASE_JOB_MAX_SECONDS", 1200) or 1200)
            idle_seconds = int(getattr(settings, "AI_TESTCASE_JOB_IDLE_SECONDS", 90) or 90)
            started_at = time.time()
            if engine_mode != "legacy":
                from ai_assistant.services.testcase_generation import TestCaseGenerationEngine
                engine = TestCaseGenerationEngine()
                buf = ""
                published_any = False
                last_flush = time.time()
                agen = engine.generate(file_path, context, requirements, cancel_event=cancel_event, user=user)
                try:
                    while True:
                        if cancel_event.is_set():
                            break
                        if max_seconds > 0 and (time.time() - started_at) > float(max_seconds):
                            raise TimeoutError("AI 生成超时，请稍后重试或检查模型配置与网络连通性。")
                        try:
                            ev = await asyncio.wait_for(agen.__anext__(), timeout=float(idle_seconds) if idle_seconds > 0 else None)
                        except StopAsyncIteration:
                            break
                        except TimeoutError:
                            raise TimeoutError("AI 生成长时间无响应，请检查模型配置与网络连通性。")
                        if cancel_event.is_set():
                            break
                        t = str(getattr(ev, "type", "") or "")
                        if t in ("progress", "meta"):
                            await _job_update_text_async(job_id, "", progress=str(getattr(ev, "message", "") or getattr(ev, "text", "") or ""))
                            continue
                        if t in ("delta", "final"):
                            if t == "final":
                                buf = str(getattr(ev, "text", "") or "")
                                await _job_update_text_async(job_id, buf)
                                buf = ""
                                continue
                            buf += str(getattr(ev, "text", "") or "")
                            if not published_any and buf:
                                await _job_update_text_async(job_id, buf)
                                buf = ""
                                published_any = True
                                last_flush = time.time()
                                continue
                            if len(buf) >= 4096 or (time.time() - last_flush) >= 0.8:
                                await _job_update_text_async(job_id, buf)
                                buf = ""
                                last_flush = time.time()
                        if t == "done":
                            break
                finally:
                    try:
                        await agen.aclose()
                    except Exception:
                        pass
                if buf:
                    await _job_update_text_async(job_id, buf)
            else:
                buf = ""
                last_flush = time.time()
                async for chunk in ai_service.generate_test_cases_stream(file_path, context, requirements, cancel_event=cancel_event, user=user):
                    if cancel_event.is_set():
                        break
                    if chunk:
                        buf += str(chunk)
                        if len(buf) >= 4096 or (time.time() - last_flush) >= 0.8:
                            await _job_update_text_async(job_id, buf)
                            buf = ""
                            last_flush = time.time()
                if buf:
                    await _job_update_text_async(job_id, buf)

        loop.run_until_complete(_run())
        loop.run_until_complete(_job_finalize_cases_async(job_id))
        status = "stopped" if cancel_event.is_set() else "done"
        loop.run_until_complete(_job_set_status_async(job_id, status))
    except Exception as e:
        try:
            msg = str(e)
            try:
                low = msg.lower()
                if ("resource_not_found_error" in low) or ("not found the model" in low):
                    try:
                        p = resolve_testcase_params(user)
                        if str(getattr(p, "provider", "") or "") == "kimi":
                            msg = (
                                (msg or "").strip()
                                + "\n提示：Kimi（Moonshot）模型名请使用 kimi-k2.5 或 moonshot-v1-8k/moonshot-v1-32k/moonshot-v1-128k（不要写“kimi 2.5”这种带空格的名称）。"
                            ).strip()
                    except Exception:
                        pass
            except Exception:
                pass
            loop.run_until_complete(_job_set_status_async(job_id, "error", error=msg))
        except Exception:
            pass
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass
        _pop_job_cancel_event(job_id)
        _pop_job_thread(job_id)
        try:
            loop.close()
        except Exception:
            pass


@csrf_exempt
@login_required
@require_POST
def job_start(request):
    uploaded_file = request.FILES.get("file")
    context = request.POST.get("context", "")
    requirements = request.POST.get("requirements", "")
    project_id = request.POST.get("project") or request.POST.get("project_id")
    requirement_id = request.POST.get("requirement") or request.POST.get("requirement_id")
    if not uploaded_file:
        return JsonResponse({"success": False, "error": "请上传文件"}, status=400)

    try:
        tc_params = resolve_testcase_params(request.user)
        try:
            setattr(request.user, "_ai_testcase_params", tc_params)
        except Exception:
            pass
    except AIKeyNotConfigured as e:
        return JsonResponse({"success": False, "error": str(e) or "请先在个人中心配置 API Key"}, status=200)

    project = None
    if project_id and str(project_id).isdigit():
        try:
            project = Project.objects.get(pk=int(project_id), id__in=visible_projects(request.user).values_list("id", flat=True))
        except Project.DoesNotExist:
            project = None

    req_obj = None
    if project and requirement_id and str(requirement_id).isdigit():
        try:
            req_obj = Requirement.objects.get(pk=int(requirement_id), project=project)
        except Requirement.DoesNotExist:
            req_obj = None

    context, requirements = _enrich_generation_inputs(project, req_obj, context, requirements)

    temp_dir = os.path.join(settings.MEDIA_ROOT, "temp", "ai_jobs")
    os.makedirs(temp_dir, exist_ok=True)
    safe_name = str(getattr(uploaded_file, "name", "") or "upload.bin").strip()[:200]
    job = AIGenerationJob.objects.create(
        user=request.user,
        project=project,
        requirement=req_obj,
        status="running",
        progress_message="start",
        source_name=safe_name,
        source_path="",
        markdown_text="",
        cases_json=[],
    )
    file_path = os.path.join(temp_dir, f"job_{job.id}_{safe_name}")
    with open(file_path, "wb+") as dst:
        for chunk in uploaded_file.chunks():
            dst.write(chunk)
    job.source_path = os.path.abspath(file_path)
    job.save(update_fields=["source_path", "updated_at"])

    cancel_event = _get_or_create_job_cancel_event(job.id)
    t = threading.Thread(target=_run_job_thread, args=(job.id, job.source_path, context, requirements, cancel_event, request.user), daemon=True)
    _set_job_thread(job.id, t)
    t.start()
    return JsonResponse({"success": True, "job_id": int(job.id)})


@csrf_exempt
@login_required
def job_status(request, job_id: int):
    try:
        job = AIGenerationJob.objects.get(id=int(job_id), user=request.user)
    except AIGenerationJob.DoesNotExist:
        return JsonResponse({"success": False, "error": "not found"}, status=404)
    data = {
        "success": True,
        "job": {
            "id": int(job.id),
            "status": str(job.status),
            "progress_message": str(job.progress_message or ""),
            "error_message": str(job.error_message or ""),
            "markdown_len": len(job.markdown_text or ""),
            "cases_count": len(job.cases_json or []),
            "project_id": int(job.project_id or 0),
            "requirement_id": int(job.requirement_id or 0),
            "source_name": str(job.source_name or ""),
            "created_at": job.created_at.isoformat() if getattr(job, "created_at", None) else "",
            "updated_at": job.updated_at.isoformat() if getattr(job, "updated_at", None) else "",
        },
    }
    if str(job.status) in ("done", "stopped"):
        data["cases"] = job.cases_json or []
        data["markdown_text"] = job.markdown_text or ""
    return JsonResponse(data)


@csrf_exempt
@login_required
def job_poll(request, job_id: int):
    try:
        job = AIGenerationJob.objects.get(id=int(job_id), user=request.user)
    except AIGenerationJob.DoesNotExist:
        return JsonResponse({"success": False, "error": "not found"}, status=404)
    try:
        offset = int(request.GET.get("offset") or 0)
    except Exception:
        offset = 0
    text = str(job.markdown_text or "")
    if offset < 0:
        offset = 0
    if offset > len(text):
        offset = len(text)
    delta = text[offset:]
    next_offset = len(text)
    return JsonResponse(
        {
            "success": True,
            "status": str(job.status),
            "progress_message": str(job.progress_message or ""),
            "error_message": str(job.error_message or ""),
            "delta": delta,
            "next_offset": next_offset,
            "cases_count": len(job.cases_json or []),
        }
    )


@csrf_exempt
@login_required
@require_POST
def job_stop(request, job_id: int):
    try:
        job = AIGenerationJob.objects.get(id=int(job_id), user=request.user)
    except AIGenerationJob.DoesNotExist:
        return JsonResponse({"success": False, "error": "not found"}, status=404)
    try:
        job.cancel_requested = True
        job.progress_message = "stop requested"
        if str(job.status) == "running":
            job.status = "stopped"
        job.save(update_fields=["cancel_requested", "progress_message", "status", "updated_at"])
    except Exception:
        pass
    ev = _get_or_create_job_cancel_event(int(job.id))
    ev.set()
    return JsonResponse({"success": True})


@csrf_exempt
@login_required
@require_POST
def job_clear(request, job_id: int):
    try:
        job = AIGenerationJob.objects.get(id=int(job_id), user=request.user)
    except AIGenerationJob.DoesNotExist:
        return JsonResponse({"success": True})
    if str(job.status) == "running":
        return JsonResponse({"success": False, "error": "任务运行中，请先停止"}, status=400)
    try:
        job.delete()
    except Exception:
        try:
            job.markdown_text = ""
            job.cases_json = []
            job.progress_message = "cleared"
            job.save(update_fields=["markdown_text", "cases_json", "progress_message", "updated_at"])
        except Exception:
            pass
    return JsonResponse({"success": True})


@csrf_exempt
@login_required
@require_POST
def job_import(request, job_id: int):
    try:
        job = AIGenerationJob.objects.get(id=int(job_id), user=request.user)
    except AIGenerationJob.DoesNotExist:
        return JsonResponse({"success": False, "error": "not found"}, status=404)
    data = {"project_id": int(job.project_id or 0), "requirement_id": int(job.requirement_id or 0), "cases": job.cases_json or [], "markdown": job.markdown_text or ""}
    request._body = json.dumps(data, ensure_ascii=False).encode("utf-8")  # type: ignore[attr-defined]
    return import_cases(request)


@csrf_exempt
@login_required
@require_POST
def job_export_excel(request, job_id: int):
    try:
        job = AIGenerationJob.objects.get(id=int(job_id), user=request.user)
    except AIGenerationJob.DoesNotExist:
        return JsonResponse({"success": False, "error": "not found"}, status=404)
    data = {"filename_prefix": "test_cases", "cases": job.cases_json or []}
    request._body = json.dumps(data, ensure_ascii=False).encode("utf-8")  # type: ignore[attr-defined]
    return export_excel(request)


@csrf_exempt
@login_required
@require_POST
def job_export_xmind(request, job_id: int):
    try:
        job = AIGenerationJob.objects.get(id=int(job_id), user=request.user)
    except AIGenerationJob.DoesNotExist:
        return JsonResponse({"success": False, "error": "not found"}, status=404)
    data = {"filename_prefix": "test_cases", "cases": job.cases_json or []}
    request._body = json.dumps(data, ensure_ascii=False).encode("utf-8")  # type: ignore[attr-defined]
    return export_xmind(request)


def sync_stream_generator(file_path, context, requirements, generation_id: str, cancel_event: threading.Event, user=None):
    """
    Synchronous wrapper for the async stream generator
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    gen = stream_generator(file_path, context, requirements, cancel_event, user=user)
    keepalive_timeout = float(getattr(settings, "AI_STREAM_KEEPALIVE_TIMEOUT_SECONDS", 5) or 5)
    progress_interval = float(getattr(settings, "AI_STREAM_PROGRESS_INTERVAL_SECONDS", 12) or 12)
    max_total = float(getattr(settings, "AI_STREAM_MAX_SECONDS", 60 * 20) or (60 * 20))
    started_at = time.time()
    last_progress_at = started_at
    next_task = None
    
    try:
        try:
            next_task = loop.create_task(gen.__anext__())
        except Exception:
            next_task = None
        while True:
            if cancel_event.is_set():
                yield json.dumps({"type": "done", "message": "stopped"}, ensure_ascii=False) + "\n"
                break
            now = time.time()
            if max_total > 0 and (now - started_at) > max_total:
                try:
                    cancel_event.set()
                except Exception:
                    pass
                yield json.dumps({"type": "error", "message": "生成超时，请缩小文件范围或降低PDF处理强度后重试"}, ensure_ascii=False) + "\n"
                yield json.dumps({"type": "done", "message": "timeout"}, ensure_ascii=False) + "\n"
                break

            if next_task is None:
                break
            done, pending = loop.run_until_complete(asyncio.wait({next_task}, timeout=keepalive_timeout))
            if not done:
                now = time.time()
                if (now - last_progress_at) >= progress_interval:
                    yield json.dumps({"type": "progress", "message": "仍在生成中…（可点击停止）"}, ensure_ascii=False) + "\n"
                    last_progress_at = now
                continue

            try:
                chunk = next_task.result()
            except StopAsyncIteration:
                break
            except Exception as e:
                yield json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False) + "\n"
                yield json.dumps({"type": "done", "message": "error"}, ensure_ascii=False) + "\n"
                break

            yield chunk
            last_progress_at = time.time()
            try:
                next_task = loop.create_task(gen.__anext__())
            except Exception:
                next_task = None
    finally:
        if next_task is not None:
            try:
                next_task.cancel()
                loop.run_until_complete(asyncio.gather(next_task, return_exceptions=True))
            except Exception:
                pass
        try:
            loop.run_until_complete(gen.aclose())
        except Exception:
            pass
        _pop_cancel_event(generation_id)
        loop.close()

@csrf_exempt
@login_required
def import_cases(request):
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            project_id = data.get('project_id')
            requirement_id = data.get('requirement_id')
            cases = data.get('cases', [])
            markdown = data.get("markdown") or ""

            if not project_id:
                return JsonResponse({'success': False, 'error': '请选择项目'})
            
            project = Project.objects.get(pk=project_id, id__in=visible_projects(request.user).values_list("id", flat=True))
            requirement = None
            if requirement_id:
                requirement = Requirement.objects.get(pk=requirement_id, project=project)

            def _parse_cases_from_markdown(markdown_text: str) -> list[dict]:
                parsed: list[dict] = []
                lines = (markdown_text or "").splitlines()
                current: dict | None = None
                in_table = False

                def save():
                    nonlocal current
                    if not current:
                        return
                    if current.get("title") and current.get("steps_list"):
                        parsed.append(current)
                    current = None

                for raw_line in lines:
                    line = raw_line.strip()
                    m = re.match(r"^#{0,6}\s*\*{0,2}TC-([\w-]+)[:：]?\s*(.*?)\*{0,2}\s*$", line, flags=re.I)
                    if m:
                        if current:
                            save()
                        current = {
                            "title": f"TC-{m.group(1)}: {m.group(2)}",
                            "pre_condition": "",
                            "priority": "",
                            "description": "",
                            "steps_list": [],
                        }
                        in_table = False
                        continue

                    if not current:
                        continue

                    m_field = re.match(r"^\s*(?:[-*]\s*)?\*{0,2}\s*优先级\s*\*{0,2}\s*[:：]\s*(.*)\s*$", line)
                    if m_field:
                        current["priority"] = (m_field.group(1) or "").strip()
                        continue

                    m_field = re.match(r"^\s*(?:[-*]\s*)?\*{0,2}\s*描述\s*\*{0,2}\s*[:：]\s*(.*)\s*$", line)
                    if m_field:
                        current["description"] = (m_field.group(1) or "").strip()
                        continue

                    m_field = re.match(r"^\s*(?:[-*]\s*)?\*{0,2}\s*前置条件\s*\*{0,2}\s*[:：]\s*(.*)\s*$", line)
                    if m_field:
                        current["pre_condition"] = (m_field.group(1) or "").strip()
                        continue

                    if re.match(r"^\s*\|?[\s\-:]+\|[\s\-:]+", line):
                        in_table = True
                        continue

                    if in_table:
                        if "|" not in line:
                            if line != "":
                                in_table = False
                            continue
                        if (("步骤" in line) or ("Step" in line)) and (("预期" in line) or ("Result" in line)):
                            continue

                        cols = [c.strip() for c in line.split("|")]
                        if cols and cols[0] == "":
                            cols = cols[1:]
                        if cols and cols[-1] == "":
                            cols = cols[:-1]
                        if len(cols) >= 2:
                            if len(cols) >= 3:
                                step = cols[1]
                                result = cols[2]
                            else:
                                step = cols[0]
                                result = cols[1]
                            if step.strip():
                                current["steps_list"].append(
                                    {"description": step.strip(), "expected_result": result.strip()}
                                )

                if current:
                    save()
                return parsed

            def _fallback_single_case(markdown_text: str) -> list[dict]:
                text = (markdown_text or "").strip()
                if not text:
                    return []
                lines = text.splitlines()
                title = ""
                for raw_line in lines[:40]:
                    line = raw_line.strip()
                    if re.match(r"^#{1,6}\s+", line):
                        title = re.sub(r"^#{1,6}\s*", "", line).strip()
                        break
                    m = re.match(r"^\s*(?:标题|用例标题)\s*[:：]\s*(.+)\s*$", line)
                    if m:
                        title = (m.group(1) or "").strip()
                        break
                if not title:
                    title = "AI生成用例"

                prio = ""
                desc = ""
                pre = ""
                for raw_line in lines[:120]:
                    line = raw_line.strip()
                    m = re.match(r"^\s*(?:[-*]\s*)?\*{0,2}\s*优先级\s*\*{0,2}\s*[:：]\s*(.*)\s*$", line)
                    if m:
                        prio = (m.group(1) or "").strip()
                        continue
                    m = re.match(r"^\s*(?:[-*]\s*)?\*{0,2}\s*描述\s*\*{0,2}\s*[:：]\s*(.*)\s*$", line)
                    if m:
                        desc = (m.group(1) or "").strip()
                        continue
                    m = re.match(r"^\s*(?:[-*]\s*)?\*{0,2}\s*前置条件\s*\*{0,2}\s*[:：]\s*(.*)\s*$", line)
                    if m:
                        pre = (m.group(1) or "").strip()
                        continue

                steps: list[dict] = []
                in_table = False
                for raw_line in lines:
                    line = raw_line.strip()
                    if re.match(r"^\s*\|?[\s\-:]+\|[\s\-:]+", line):
                        in_table = True
                        continue
                    if in_table:
                        if "|" not in line:
                            if line != "":
                                in_table = False
                            continue
                        if (("步骤" in line) or ("Step" in line)) and (("预期" in line) or ("Result" in line)):
                            continue
                        cols = [c.strip() for c in line.split("|")]
                        if cols and cols[0] == "":
                            cols = cols[1:]
                        if cols and cols[-1] == "":
                            cols = cols[:-1]
                        if len(cols) >= 2:
                            if len(cols) >= 3:
                                step = cols[1]
                                result = cols[2]
                            else:
                                step = cols[0]
                                result = cols[1]
                            if step.strip():
                                steps.append({"description": step.strip(), "expected_result": result.strip()})

                if not steps:
                    return []
                return [
                    {
                        "title": f"TC-001: {title}",
                        "pre_condition": pre,
                        "priority": prio,
                        "description": desc,
                        "steps_list": steps,
                    }
                ]

            if not cases and markdown.strip():
                cases = _parse_cases_from_markdown(markdown)
                if not cases:
                    cases = _fallback_single_case(markdown)

            if not cases:
                return JsonResponse({'success': False, 'error': '未检测到有效的测试用例格式，无法导入。'})

            cases = dedup_cases(cases)

            created_count = 0
            for case_data in cases:
                prio_raw = str(case_data.get("priority") or "").strip()
                priority = 2
                if prio_raw in ("高", "High", "HIGH", "P1", "1"):
                    priority = 1
                elif prio_raw in ("低", "Low", "LOW", "P3", "3"):
                    priority = 3
                # Create TestCase
                test_case = TestCase.objects.create(
                    project=project,
                    requirement=requirement,
                    title=str(case_data.get('title', 'AI生成用例'))[:200],
                    pre_condition=case_data.get('pre_condition', ''),
                    type=1, # Functional
                    priority=priority,
                    status=0, # Not Executed
                    creator=request.user
                )
                
                # Create TestCaseSteps
                # steps_data should be a list of objects: [{'description': '...', 'expected_result': '...'}]
                steps_list = case_data.get('steps_list', [])
                for i, step in enumerate(steps_list):
                    TestCaseStep.objects.create(
                        case=test_case,
                        step_number=i + 1,
                        description=step.get('description', ''),
                        expected_result=step.get('expected_result', ''),
                        smart_data_enabled=False,
                        is_executed=False
                    )
                
                created_count += 1

            return JsonResponse({'success': True, 'count': created_count})
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    return JsonResponse({'success': False, 'error': '仅支持POST请求'})


@login_required
@require_POST
def export_excel(request):
    try:
        data = json.loads(request.body or "{}")
        cases = data.get("cases") or []
        prefix = data.get("filename_prefix") or "test_cases"
        content = generate_xlsx_bytes(cases)
        filename = build_export_filename(prefix, "xlsx")
        resp = HttpResponse(
            content,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)


@login_required
@require_POST
def export_xmind(request):
    try:
        data = json.loads(request.body or "{}")
        cases = data.get("cases") or []
        prefix = data.get("filename_prefix") or "test_cases"
        content = generate_xmind_bytes(cases)
        filename = build_export_filename(prefix, "xmind")
        resp = HttpResponse(content, content_type="application/vnd.xmind.workbook")
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=400)
