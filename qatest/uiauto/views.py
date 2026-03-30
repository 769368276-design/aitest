from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from core.pagination import paginate
from core.visibility import visible_projects
from testcases.models import TestCase
from uiauto.models import UIAutoExecution
from uiauto.services.execution_queue import enqueue_execution


@login_required
@require_GET
def entry(request):
    projects = visible_projects(request.user)
    cases = TestCase.objects.filter(project__in=projects).select_related("project", "creator").order_by("-id")
    project_id = str(request.GET.get("project_id") or "").strip()
    q = str(request.GET.get("q") or "").strip()
    if project_id.isdigit():
        cases = cases.filter(project_id=int(project_id))
    if q:
        cases = cases.filter(title__icontains=q)
    page_obj = paginate(request, cases, per_page=20)
    return render(request, "uiauto/entry.html", {"projects": projects.order_by("name"), "page_obj": page_obj, "q": q, "project_id": project_id})


@login_required
@require_POST
def run_case(request, case_id: int):
    case = get_object_or_404(TestCase.objects.filter(project__in=visible_projects(request.user)), pk=case_id)
    execution = UIAutoExecution.objects.create(case=case, executor=request.user, status="pending", result_summary={})
    enqueue_execution(execution.id)
    return JsonResponse({"success": True, "execution_id": execution.id})


@login_required
@require_POST
def batch_run(request):
    try:
        case_ids = request.POST.getlist("case_ids") or []
        case_ids = [int(x) for x in case_ids if str(x).isdigit()]
    except Exception:
        case_ids = []
    if not case_ids:
        return JsonResponse({"success": False, "error": "请选择至少一个用例"}, status=400)
    cases = list(TestCase.objects.filter(project__in=visible_projects(request.user), id__in=case_ids))
    if not cases:
        return JsonResponse({"success": False, "error": "未找到可执行用例"}, status=404)
    execution_ids = []
    for c in cases:
        execution = UIAutoExecution.objects.create(case=c, executor=request.user, status="pending", result_summary={})
        enqueue_execution(execution.id)
        execution_ids.append(int(execution.id))
    return JsonResponse({"success": True, "execution_ids": execution_ids, "execution_id": execution_ids[0]})


@login_required
@require_GET
def console(request, execution_id: int):
    execution = get_object_or_404(UIAutoExecution.objects.select_related("case"), pk=execution_id, case__project__in=visible_projects(request.user))
    return render(request, "uiauto/console.html", {"execution": execution, "poll_ms": 1000})


@login_required
@require_GET
def status(request, execution_id: int):
    execution = get_object_or_404(UIAutoExecution.objects.select_related("case"), pk=execution_id, case__project__in=visible_projects(request.user))
    steps_qs = execution.step_records.select_related("step").order_by("step_number", "id")
    steps = []
    for s in steps_qs:
        steps.append(
            {
                "id": s.id,
                "step_number": s.step_number,
                "description": s.description,
                "expected_result": s.expected_result or "",
                "status": s.status,
                "error_message": s.error_message or "",
                "metrics": s.metrics or {},
                "screenshot_after": s.screenshot_after.url if s.screenshot_after else "",
            }
        )
    return JsonResponse({"execution_id": execution.id, "status": execution.status, "summary": execution.result_summary or {}, "steps": steps})


@login_required
@require_POST
def pause(request, execution_id: int):
    execution = get_object_or_404(UIAutoExecution, pk=execution_id, case__project__in=visible_projects(request.user))
    execution.pause_signal = True
    execution.save(update_fields=["pause_signal"])
    return JsonResponse({"success": True})


@login_required
@require_POST
def resume(request, execution_id: int):
    execution = get_object_or_404(UIAutoExecution, pk=execution_id, case__project__in=visible_projects(request.user))
    execution.pause_signal = False
    execution.save(update_fields=["pause_signal"])
    return JsonResponse({"success": True})


@login_required
@require_POST
def stop(request, execution_id: int):
    execution = get_object_or_404(UIAutoExecution, pk=execution_id, case__project__in=visible_projects(request.user))
    execution.stop_signal = True
    execution.pause_signal = False
    execution.save(update_fields=["stop_signal", "pause_signal"])
    return JsonResponse({"success": True})


@login_required
@require_GET
def report_list(request):
    projects = visible_projects(request.user)
    executions = UIAutoExecution.objects.filter(case__project__in=projects).select_related("case", "executor").order_by("-id")
    case_id = str(request.GET.get("case_id") or "").strip()
    if case_id.isdigit():
        executions = executions.filter(case_id=int(case_id))
    page_obj = paginate(request, executions, per_page=20)
    return render(request, "uiauto/report_list.html", {"page_obj": page_obj, "selected_case_id": case_id})


@login_required
@require_GET
def report_detail(request, execution_id: int):
    execution = get_object_or_404(UIAutoExecution.objects.select_related("case", "executor"), pk=execution_id, case__project__in=visible_projects(request.user))
    steps = execution.step_records.select_related("step").order_by("step_number", "id")
    return render(request, "uiauto/report_detail.html", {"execution": execution, "steps": steps})

